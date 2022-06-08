# coding=utf-8
# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""GPT Non-Causal Mask Language Model Finetune Style dataset."""

import os
import time
import random
import collections

import numpy as np
import torch

from megatron import mpu, print_rank_0, get_tokenizer
from megatron.data.blendable_dataset import BlendableDataset
from megatron.data.dataset_utils import get_datasets_weights_and_num_samples, create_masked_lm_predictions
from megatron.data.dataset_utils import get_train_valid_test_split_, get_split_by_range_, get_indexed_dataset_
from megatron.data.indexed_dataset import make_dataset as make_indexed_dataset


def build_train_valid_test_datasets(data_prefix, data_impl, splits_string,
                                    train_valid_test_num_samples,
                                    max_seq_length,
                                    masked_lm_prob, seed,
                                    skip_warmup
                                    ):
    if len(data_prefix) == 1:
        return _build_train_valid_test_datasets(data_prefix[0],
                                                data_impl, splits_string,
                                                train_valid_test_num_samples,
                                                max_seq_length, masked_lm_prob,
                                                seed, skip_warmup
                                                )
    # Blending dataset.
    # Parse the values.
    output = get_datasets_weights_and_num_samples(data_prefix,
                                                  train_valid_test_num_samples)
    prefixes, weights, datasets_train_valid_test_num_samples = output

    # Build individual datasets.
    train_datasets = []
    valid_datasets = []
    test_datasets = []
    for i in range(len(prefixes)):
        train_ds, valid_ds, test_ds = _build_train_valid_test_datasets(
            prefixes[i], data_impl, splits_string,
            datasets_train_valid_test_num_samples[i],
            max_seq_length, masked_lm_prob,
            seed, skip_warmup)
        if train_ds:
            train_datasets.append(train_ds)
        if valid_ds:
            valid_datasets.append(valid_ds)
        if test_ds:
            test_datasets.append(test_ds)

        # Blend.
    blending_train_dataset = None
    if train_datasets:
        blending_train_dataset = BlendableDataset(train_datasets, weights)
    blending_valid_dataset = None
    if valid_datasets:
        blending_valid_dataset = BlendableDataset(valid_datasets, weights)
    blending_test_dataset = None
    if test_datasets:
        blending_test_dataset = BlendableDataset(test_datasets, weights)

    return (blending_train_dataset, blending_valid_dataset,
            blending_test_dataset)


def _build_train_valid_test_datasets(data_prefix, data_impl, splits_string,
                                     train_valid_test_num_samples,
                                     max_seq_length, masked_lm_prob, seed,
                                     skip_warmup):
    """Build train, valid, and test datasets."""


    # Indexed dataset.
    indexed_dataset = get_indexed_dataset_(data_prefix,
                                           data_impl,
                                           skip_warmup)

    total_num_of_documents = indexed_dataset.sizes.shape[0] - 1
    splits = get_train_valid_test_split_(splits_string, total_num_of_documents)
    # Print stats about the splits.
    print_rank_0(' > dataset split:')

    def print_split_stats(name, index):
        print_rank_0('    {}:'.format(name))
        print_rank_0('     document indices in [{}, {}) total of {} '
                     'documents'.format(splits[index], splits[index + 1],
                                        splits[index + 1] - splits[index]))
        start_index = indexed_dataset.doc_idx[splits[index]]
        end_index = indexed_dataset.doc_idx[splits[index + 1]]
        print_rank_0('     sentence indices in [{}, {}) total of {} '
                     'sentences'.format(start_index, end_index,
                                        end_index - start_index))
    print_split_stats('train', 0)
    print_split_stats('validation', 1)
    print_split_stats('test', 2)

    def build_dataset(index, name):
        dataset = None
        if splits[index + 1] > splits[index]:
            # Get the pointer to the original doc-idx so we can set it later.
            doc_idx_ptr = indexed_dataset.get_doc_idx()
            # Slice the doc-idx
            start_index = splits[index]
            # Add +1 so we can index into the dataset to get the upper bound.
            end_index = splits[index + 1] + 1
            # New doc_idx view.
            indexed_dataset.set_doc_idx(doc_idx_ptr[start_index:end_index])
            # Build the dataset accordingly.
            kwargs = dict(
                name=name,
                data_prefix=data_prefix,
                max_seq_length=max_seq_length,
                seed=seed,
            )
            dataset = NonCausalMLMDataset(
                    indexed_dataset=indexed_dataset,
                    masked_lm_prob=masked_lm_prob,
                    **kwargs
            )
            indexed_dataset.set_doc_idx(doc_idx_ptr)
            # Checks.
            # assert indexed_dataset.doc_idx[0] == 0
            # assert indexed_dataset.doc_idx.shape[0] == \
            #     (total_num_of_documents + 1)
        return dataset

    train_dataset = build_dataset(0, 'train')
    valid_dataset = build_dataset(1, 'valid')
    test_dataset = build_dataset(2, 'test')

    return (train_dataset, valid_dataset, test_dataset)


class NonCausalMLMDataset(torch.utils.data.Dataset):

    def __init__(self, name, indexed_dataset, data_prefix,
                 masked_lm_prob,
                 max_seq_length,
                 seed):

        # Params to store.
        self.name = name
        self.seed = seed
        self.masked_lm_prob = masked_lm_prob
        self.max_seq_length = max_seq_length

        # Dataset.
        self.indexed_dataset = indexed_dataset

        max_ngrams = 3
        # T5-like span masked language modeling will fuse consecutively masked tokens to a single sentinel token.
        # To ensure that the input length is `max_seq_length`, we need to increase the maximum length
        # according to `masked_lm_prob` and `max_ngrams`. We can also define the label length accordingly.
        expanded_inputs_length, targets_length = compute_input_and_target_lengths(
            self.max_seq_length,
            self.masked_lm_prob,
            max_ngrams
            )
        self.expanded_inputs_length = expanded_inputs_length
        self.targets_length = targets_length

        # Build the samples mapping.
        self.samples_mapping = get_samples_mapping(
            self.indexed_dataset,
            data_prefix,
            self.name,
            max_len=expanded_inputs_length
            )

        # Vocab stuff.
        tokenizer = get_tokenizer()
        self.vocab_id_list = list(tokenizer.inv_vocab.keys())
        self.vocab_id_to_token_dict = tokenizer.inv_vocab
        self.cls_id = tokenizer.cls
        self.sep_id = tokenizer.sep
        self.mask_id = tokenizer.mask
        self.pad_id = tokenizer.pad
        self.bos_id = tokenizer.bos_token_id
        self.eos_id = tokenizer.eos_token_id
        self.sentinel_tokens = tokenizer.additional_special_tokens_ids
        assert len(self.sentinel_tokens) > 0, "Provide the argument --vocab-extra-ids 100 to the script"

    def __len__(self):
        return len(self.samples_mapping)

    def __getitem__(self, idx):

        indices = self.samples_mapping[idx]
        sample = []
        for doc_idx, start_index, end_index in indices:
            sample.append(self.indexed_dataset.get(doc_idx)[start_index:end_index])

        return build_training_sample(
            sample, self.expanded_inputs_length, self.vocab_id_list,
            self.cls_id, self.sep_id, self.mask_id, self.pad_id, self.bos_id, self.eos_id,
            self.sentinel_tokens
            )


def build_training_sample(
    sample, expanded_inputs_length, vocab_id_list,
    cls_id, sep_id, mask_id, pad_id, bos_id=None, eos_id=None, sentinel_tokens=None
    ):
    """Build training sample.

    Arguments:
        TODO: Add description
    """

    # flatten sentences into one list
    tokens = [token for sentence in sample for token in sentence]

    mask_indices = np.asarray([random_spans_noise_mask(
        expanded_inputs_length,
        noise_density=0.15,
        mean_noise_span_length=3
        )])
    labels_mask = ~mask_indices
    
    input_ids_sentinel = create_sentinel_ids(mask_indices.astype(np.int8), vocab_len=len(vocab_id_list))
    labels_sentinel = create_sentinel_ids(labels_mask.astype(np.int8), vocab_len=len(vocab_id_list))

    tokens = np.asarray([tokens])
    input_tokens_ids = filter_input_ids(tokens, input_ids_sentinel, eos_id)[0]
    output_tokens_ids = filter_input_ids(tokens, labels_sentinel, eos_id)[0]

    text_tokens_ids = np.concatenate((input_tokens_ids, output_tokens_ids))

    prefix_len = len(input_tokens_ids)

    return {
        'text': text_tokens_ids,
        'prefix_len': prefix_len
    }


def get_samples_mapping(indexed_dataset, data_prefix, name, max_len):

    def breakdown(sample_len, idx_offset=None, idx_list=None, max_len=None):

        if idx_list is None:
            idx_list = []

        if idx_offset is None:
            idx_offset = 0

        if sample_len < max_len:
            idx_list.append(idx_offset+sample_len)
        else:
            sample_len = sample_len - max_len
            idx_list.append(idx_offset+max_len)
            idx_offset += max_len

            breakdown(sample_len, idx_offset=idx_offset, idx_list=idx_list)

        idx_list = [0]+idx_list
        return list(zip(idx_list[:-1], idx_list[1:]))


    # Filename of the index mapping
    indexmap_filename = data_prefix
    indexmap_filename += '_{}_indexmap'.format(name)
    indexmap_filename += '.npy'

    # Build the indexed mapping if not exist.
    if torch.distributed.get_rank() == 0 and \
       not os.path.isfile(indexmap_filename):
        print(' > WARNING: could not find index map file {}, building '
              'the indices on rank 0 ...'.format(indexmap_filename))

        # Make sure the types match the helpers input types.
        assert indexed_dataset.doc_idx.dtype == np.int64
        assert indexed_dataset.sizes.dtype == np.int32

        # Build samples mapping
        verbose = torch.distributed.get_rank() == 0
        start_time = time.time()
        print_rank_0(' > building sapmles index mapping for {} ...'.format(
            name))
        samples_mapping = []
        sample_indices = []
        doc_idx = 0
        current_len = 0
        _idx = 0
        for doc_idx, sample_len in zip(indexed_dataset.doc_idx, indexed_dataset.sizes):
            _idx = 0

            if current_len + sample_len > max_len:
                end_idx = max_len - current_len
                sample_indices.append([doc_idx, 0, end_idx])
                samples_mapping.append(sample_indices)
                sample_indices = []
                current_len = 0
                sample_len -= end_idx
                _idx = end_idx

            break_len = current_len + sample_len

            indices = breakdown(sample_len, max_len=max_len)
            for _start_idx, _end_idx in indices:
                _len = _end_idx - _start_idx
                if _len == max_len:
                    samples_mapping.append([[doc_idx, _start_idx+_idx, _end_idx+_idx]])
                else:
                    sample_indices.append([doc_idx, _start_idx+_idx, _end_idx+_idx])
                    current_len += _len

        print_rank_0(' > done building sapmles index maping')
        np.save(indexmap_filename, samples_mapping, allow_pickle=True)
        print_rank_0(' > saved the index mapping in {}'.format(
            indexmap_filename))
        # Make sure all the ranks have built the mapping
        print_rank_0(' > elasped time to build and save samples mapping '
                     '(seconds): {:4f}'.format(
                         time.time() - start_time))
    # This should be a barrier but nccl barrier assumes
    # device_index=rank which is not the case for model
    # parallel case
    counts = torch.cuda.LongTensor([1])
    torch.distributed.all_reduce(counts, group=mpu.get_data_parallel_group())
    torch.distributed.all_reduce(counts, group=mpu.get_pipeline_model_parallel_group())
    assert counts[0].item() == (
        torch.distributed.get_world_size() //
        torch.distributed.get_world_size(group=mpu.get_tensor_model_parallel_group()))

    # Load indexed dataset.
    print_rank_0(' > loading indexed mapping from {}'.format(
        indexmap_filename))
    start_time = time.time()
    samples_mapping = np.load(indexmap_filename, allow_pickle=True)
    print_rank_0('    loaded indexed file in {:3.3f} seconds'.format(
        time.time() - start_time))
    print_rank_0('    total number of samples: {}'.format(
        len(samples_mapping)))

    return samples_mapping


def create_sentinel_ids(mask_indices, vocab_len):
    """
    Sentinel ids creation given the indices that should be masked.
    The start indices of each mask are replaced by the sentinel ids in increasing
    order. Consecutive mask indices to be deleted are replaced with `-1`.
    """
    start_indices = mask_indices - np.roll(mask_indices, 1, axis=-1) * mask_indices
    start_indices[:, 0] = mask_indices[:, 0]

    sentinel_ids = np.where(start_indices != 0, np.cumsum(start_indices, axis=-1), start_indices)
    sentinel_ids = np.where(sentinel_ids != 0, (vocab_len - sentinel_ids), 0)
    sentinel_ids -= mask_indices - start_indices

    return sentinel_ids


def filter_input_ids(input_ids, sentinel_ids, eos_id):
    """
    Puts sentinel mask on `input_ids` and fuse consecutive mask tokens into a single mask token by deleting.
    This will reduce the sequence length from `expanded_inputs_length` to `input_length`.
    """
    batch_size = input_ids.shape[0]

    input_ids_full = np.where(sentinel_ids != 0, sentinel_ids, input_ids)
    # input_ids tokens and sentinel tokens are >= 0, tokens < 0 are
    # masked tokens coming after sentinel tokens and should be removed
    input_ids = input_ids_full[input_ids_full >= 0].reshape((batch_size, -1))
    input_ids = np.concatenate(
        [input_ids, np.full((batch_size, 1), eos_id, dtype=np.int32)], axis=-1
    )
    return input_ids


def compute_input_and_target_lengths(inputs_length, noise_density, mean_noise_span_length):
    """This function is copy of `random_spans_helper <https://github.com/google-research/text-to-text-transfer-transformer/blob/84f8bcc14b5f2c03de51bd3587609ba8f6bbd1cd/t5/data/preprocessors.py#L2466>`__ .
    Training parameters to avoid padding with random_spans_noise_mask.
    When training a model with random_spans_noise_mask, we would like to set the other
    training hyperparmeters in a way that avoids padding.
    This function helps us compute these hyperparameters.
    We assume that each noise span in the input is replaced by extra_tokens_per_span_inputs sentinel tokens,
    and each non-noise span in the targets is replaced by extra_tokens_per_span_targets sentinel tokens.
    This function tells us the required number of tokens in the raw example (for split_tokens())
    as well as the length of the encoded targets. Note that this function assumes
    the inputs and targets will have EOS appended and includes that in the reported length.
    Args:
        inputs_length: an integer - desired length of the tokenized inputs sequence
        noise_density: a float
        mean_noise_span_length: a float
    Returns:
        tokens_length: length of original text in tokens
        targets_length: an integer - length in tokens of encoded targets sequence
    """

    def _tokens_length_to_inputs_length_targets_length(tokens_length):
        num_noise_tokens = int(round(tokens_length * noise_density))
        num_nonnoise_tokens = tokens_length - num_noise_tokens
        num_noise_spans = int(round(num_noise_tokens / mean_noise_span_length))
        # inputs contain all nonnoise tokens, sentinels for all noise spans
        # and one EOS token.
        _input_length = num_nonnoise_tokens + num_noise_spans + 1
        _output_length = num_noise_tokens + num_noise_spans + 1
        return _input_length, _output_length

    tokens_length = inputs_length

    while _tokens_length_to_inputs_length_targets_length(tokens_length + 1)[0] <= inputs_length:
        tokens_length += 1

    inputs_length, targets_length = _tokens_length_to_inputs_length_targets_length(tokens_length)

    # minor hack to get the targets length to be equal to inputs length
    # which is more likely to have been set to a nice round number.
    if noise_density == 0.5 and targets_length > inputs_length:
        tokens_length -= 1
        targets_length -= 1
    return tokens_length, targets_length


def random_spans_noise_mask(
    length,
    noise_density=0.15,
    mean_noise_span_length=3
    ):

    """This function is copy of `random_spans_helper <https://github.com/google-research/text-to-text-transfer-transformer/blob/84f8bcc14b5f2c03de51bd3587609ba8f6bbd1cd/t5/data/preprocessors.py#L2682>`__ .
    Noise mask consisting of random spans of noise tokens.
    The number of noise tokens and the number of noise spans and non-noise spans
    are determined deterministically as follows:
    num_noise_tokens = round(length * noise_density)
    num_nonnoise_spans = num_noise_spans = round(num_noise_tokens / mean_noise_span_length)
    Spans alternate between non-noise and noise, beginning with non-noise.
    Subject to the above restrictions, all masks are equally likely.
    Args:
        length: an int32 scalar (length of the incoming token sequence)
        noise_density: a float - approximate density of output mask
        mean_noise_span_length: a number
    Returns:
        a boolean tensor with shape [length]
    """

    orig_length = length

    num_noise_tokens = int(np.round(length * noise_density))
    # avoid degeneracy by ensuring positive numbers of noise and nonnoise tokens.
    num_noise_tokens = min(max(num_noise_tokens, 1), length - 1)
    num_noise_spans = int(np.round(num_noise_tokens / mean_noise_span_length))

    # avoid degeneracy by ensuring positive number of noise spans
    num_noise_spans = max(num_noise_spans, 1)
    num_nonnoise_tokens = length - num_noise_tokens

    # pick the lengths of the noise spans and the non-noise spans
    def _random_segmentation(num_items, num_segments):
        """Partition a sequence of items randomly into non-empty segments.
        Args:
            num_items: an integer scalar > 0
            num_segments: an integer scalar in [1, num_items]
        Returns:
            a Tensor with shape [num_segments] containing positive integers that add
            up to num_items
        """
        mask_indices = np.arange(num_items - 1) < (num_segments - 1)
        np.random.shuffle(mask_indices)
        first_in_segment = np.pad(mask_indices, [[1, 0]])
        segment_id = np.cumsum(first_in_segment)
        # count length of sub segments assuming that list is sorted
        _, segment_length = np.unique(segment_id, return_counts=True)
        return segment_length

    noise_span_lengths = _random_segmentation(num_noise_tokens, num_noise_spans)
    nonnoise_span_lengths = _random_segmentation(num_nonnoise_tokens, num_noise_spans)

    interleaved_span_lengths = np.reshape(
        np.stack([nonnoise_span_lengths, noise_span_lengths], axis=1), [num_noise_spans * 2]
    )
    span_starts = np.cumsum(interleaved_span_lengths)[:-1]
    span_start_indicator = np.zeros((length,), dtype=np.int8)
    span_start_indicator[span_starts] = True
    span_num = np.cumsum(span_start_indicator)
    is_noise = np.equal(span_num % 2, 1)

    return is_noise[:orig_length]