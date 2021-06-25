# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
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
"""
Fine-tuning the library models for language modeling on a text file (GPT, GPT-2, BERT, RoBERTa).
GPT and GPT-2 are fine-tuned using a causal language modeling (CLM) loss while BERT and RoBERTa are fine-tuned
using a masked language modeling (MLM) loss.
"""

from __future__ import absolute_import, division, print_function

import argparse
import glob
import logging
import os
import pickle
import random
import re
import shutil

import numpy as np
import torch
from torch.utils.data import DataLoader, Dataset, SequentialSampler, RandomSampler
from torch.utils.data.distributed import DistributedSampler

import onnx
import onnxruntime as  ort
from torch.nn import CrossEntropyLoss

from tqdm import tqdm, trange

from transformers import (GPT2Config, GPT2LMHeadModel, GPT2Tokenizer)


logger = logging.getLogger(__name__)


MODEL_CLASSES = {
    'gpt2': (GPT2Config, GPT2LMHeadModel, GPT2Tokenizer)
}

class TextDataset(Dataset):
    def __init__(self, tokenizer, args, file_path='train', block_size=512):
        assert os.path.isfile(file_path)
        directory, filename = os.path.split(file_path)
        if not os.path.exists("./dataset_cached"):
            os.makedirs("./dataset_cached")
        cached_features_file = os.path.join("./dataset_cached", 
            args.model_name_or_path + '_cached_lm_' + str(block_size) + '_' + filename)

        if os.path.exists(cached_features_file) and not args.overwrite_cache:
            logger.info("Loading features from cached file %s", cached_features_file)
            with open(cached_features_file, 'rb') as handle:
                self.examples = pickle.load(handle)
        else:
            logger.info("Creating features from dataset file at %s", directory)

            self.examples = []
            with open(file_path, encoding="utf-8") as f:
                text = f.read()

            tokenized_text = tokenizer.convert_tokens_to_ids(tokenizer.tokenize(text))

            for i in range(0, len(tokenized_text)-block_size+1, block_size): # Truncate in block of block_size
                self.examples.append(tokenizer.build_inputs_with_special_tokens(tokenized_text[i:i+block_size]))
            # Note that we are loosing the last truncated example here for the sake of simplicity (no padding)
            # If your dataset is small, first you should loook for a bigger one :-) and second you
            # can change this behavior by adding (model specific) padding.

            logger.info("Saving features into cached file %s", cached_features_file)
            with open(cached_features_file, 'wb') as handle:
                pickle.dump(self.examples, handle, protocol=pickle.HIGHEST_PROTOCOL)

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, item):
        return torch.tensor(self.examples[item])


def load_and_cache_examples(args, tokenizer, evaluate=False):
    dataset = TextDataset(tokenizer, args, file_path=args.eval_data_file, block_size=args.block_size)
    return dataset

def evaluate(args, model, tokenizer, prefix=""):
    eval_dataset = load_and_cache_examples(args, tokenizer, evaluate=True)

    args.eval_batch_size = args.per_gpu_eval_batch_size * max(1, args.n_gpu)
    eval_sampler = SequentialSampler(eval_dataset)
    eval_dataloader = DataLoader(eval_dataset, sampler=eval_sampler, batch_size=args.eval_batch_size)

    # multi-gpu evaluate
    if args.n_gpu > 1:
        model = torch.nn.DataParallel(model)

    # Eval!
    logger.info("***** Running evaluation {} *****".format(prefix))
    logger.info("  Num examples = %d", len(eval_dataset))
    logger.info("  Batch size = %d", args.eval_batch_size)
    eval_loss = 0.0
    nb_eval_steps = 0
    import timeit
    total_time = 0.0

    options = ort.SessionOptions()
    session = ort.InferenceSession(model.SerializeToString(), options)
    len_outputs = len(session.get_outputs())
    len_inputs = len(session.get_inputs())
    inputs_names = [session.get_inputs()[i].name for i in range(len_inputs)]
    ort_inputs = {}

    for idx, batch in enumerate(tqdm(eval_dataloader, desc="Evaluating")):
        if nb_eval_steps >= args.warmup_steps:
            start = timeit.default_timer()
        inputs, labels = (batch, batch)
        inputs = inputs.to(args.device)
        labels = labels.to(args.device)
        for i in range(len_inputs):
            inputs = np.array(inputs)
            inputs = np.expand_dims(inputs, axis=0)
            ort_inputs.update({inputs_names[i]: inputs})
        predictions = session.run(None, ort_inputs)
        lm_logits = predictions[0]
        lm_logits = torch.from_numpy(lm_logits)
        # Shift so that tokens < n predict n
        shift_logits = lm_logits[..., :-1, :].contiguous()
        shift_labels = labels[..., 1:].contiguous()
        # Flatten the tokens
        loss_fct = CrossEntropyLoss(ignore_index=-1)
        lm_loss = loss_fct(shift_logits.view(-1, shift_logits.size(-1)),
                        shift_labels.view(-1))
        
        if nb_eval_steps >= args.warmup_steps:
            total_time += (timeit.default_timer() - start)
        eval_loss += lm_loss.mean().item()
        nb_eval_steps += 1

        if args.iter > 0 and nb_eval_steps > (args.warmup_steps + args.iter):
            break

    if nb_eval_steps >= args.warmup_steps:
        perf = (nb_eval_steps - args.warmup_steps) * args.eval_batch_size / total_time
        if args.eval_batch_size == 1:
            print('Latency: %.3f ms' % (total_time / (nb_eval_steps - args.warmup_steps) * 1000))
        print("Throughput: {} samples/s".format(perf))
    else:
        logger.info("*****no performance, please check dataset length and warmup number *****")

    eval_loss = eval_loss / nb_eval_steps
    perplexity = torch.exp(torch.tensor(eval_loss))

    result = {
        "perplexity": perplexity
    }
    logger.info("***** Eval results {} *****".format(prefix))
    for key in sorted(result.keys()):
        logger.info("  %s = %s", key, str(result[key]))
    
    if args.benchmark and args.mode == "accuracy":
        print("Batch size = %d" % args.eval_batch_size)
        print("Accuracy: %.5f" % (100 - result['perplexity']))
    
    return 100 - result['perplexity']


def main():
    parser = argparse.ArgumentParser()

    ## Required parameters
    parser.add_argument('--model_path', type=str,  required=True, 
                        help='Pre-trained bert model onnx file.')
    parser.add_argument("--eval_data_file", type=str, required=True,
                        help="Input evaluation data file to evaluate the perplexity on (a text file).")

    ## Other parameters
    parser.add_argument("--model_type", type=str,
                        help="The model architecture to be fine-tuned.")
    parser.add_argument("--model_name_or_path", type=str,
                        help="The model checkpoint for weights initialization.")
    parser.add_argument("--cache_dir", default="", type=str,
                        help="Optional directory to store the pre-trained models downloaded from s3 (instread of the default one)")
    parser.add_argument("--block_size", default=-1, type=int,
                        help="Optional input sequence length after tokenization."
                             "The training dataset will be truncated in block of this size for training."
                             "Default to the model max input length for single sentence inputs (take into account special tokens).")
    parser.add_argument("--per_gpu_eval_batch_size", default=4, type=int,
                        help="Batch size per GPU/CPU for evaluation.")
    parser.add_argument('--overwrite_cache', action='store_true',
                        help="Overwrite the cached training and evaluation sets")
    parser.add_argument('--tune',action='store_true', default=False,
                        help='Get bert tuning quantization model with lpot.')
    parser.add_argument('--config',type=str,
                        help='Tuning config file path')
    parser.add_argument('--output_model',type=str, default='gpt2_tune.onnx',
                        help='output model path and name')
    parser.add_argument('--benchmark',action='store_true', default=False,
                        help='Get benchmark performance of quantized model.')
    parser.add_argument('--mode', type=str, 
                        help="benchmark mode of performance or accuracy")
    parser.add_argument("--warmup_steps", default=10, type=int,
                        help="Linear warmup over warmup_steps.")
    parser.add_argument('-i', "--iter", default=0, type=int,
                        help='For accuracy measurement only.')
    args = parser.parse_args()

    device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
    args.n_gpu = torch.cuda.device_count()
    args.device = device

    # Setup logging
    logging.basicConfig(format = '%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                        datefmt = '%m/%d/%Y %H:%M:%S',
                        level = logging.INFO)

    config_class, model_class, tokenizer_class = MODEL_CLASSES[args.model_type]
    config = config_class.from_pretrained(args.model_name_or_path,
                                          cache_dir=args.cache_dir if args.cache_dir else None)
    tokenizer = tokenizer_class.from_pretrained(args.model_name_or_path,
                                                do_lower_case=False, 
                                                cache_dir=args.cache_dir if args.cache_dir else None)
    if args.block_size <= 0:
        args.block_size = tokenizer.max_len_single_sentence  # Our input block size will be the max possible for the model
    args.block_size = min(args.block_size, tokenizer.max_len_single_sentence)
    model = model_class.from_pretrained(args.model_name_or_path,
                                        from_tf=bool('.ckpt' in args.model_name_or_path),
                                        config=config,
                                        cache_dir=args.cache_dir if args.cache_dir else None)
    model.to(args.device)
    
    logger.info("Training/evaluation parameters %s", args)
            
    model = onnx.load(args.model_path)
    ds = load_and_cache_examples(args, tokenizer, evaluate=True)

    def eval_func(model):
        return evaluate(args, model, tokenizer)

    if args.benchmark:
        evaluate(args, model, tokenizer)
        
    if args.tune:
        from lpot.experimental import Quantization, common
        quantize = Quantization(args.config)
        quantize.model = common.Model(model)
        quantize.calib_dataloader = common.DataLoader(ds, batch_size=args.per_gpu_eval_batch_size)
        quantize.eval_func = eval_func
        q_model = quantize()
        q_model.save(args.output_model)


if __name__ == "__main__":
    main()