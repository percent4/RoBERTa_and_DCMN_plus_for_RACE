# -*- coding: utf-8 -*-
# @Time : 2021/4/14 16:58
# @Author : Jclian91
# @File : dcmn_roberta.py
# @Place : Yangpu, Shanghai
"""BERT finetuning runner."""

import pandas as pd
import json
import time

import logging
import os
import argparse
import random
from tqdm import tqdm, trange

import numpy as np
import torch
from torch import nn
from torch.autograd import Variable
from torch.nn import CrossEntropyLoss
from torch.utils.data import TensorDataset, DataLoader, RandomSampler, SequentialSampler
from torch.utils.data.distributed import DistributedSampler

from pytorch_pretrained_bert.tokenization import BertTokenizer
from pytorch_pretrained_bert.optimization import BertAdam
from pytorch_pretrained_bert.modeling import PreTrainedBertModel, BertModel, BertConfig, BertPooler
from pytorch_pretrained_bert.file_utils import PYTORCH_PRETRAINED_BERT_CACHE

from mctest import parse_mc

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.INFO)
logger = logging.getLogger(__name__)
TASK_NAME = 'mctest'  # 'roc' 'coin' 'race' 'mctest'
NUM_CHOICE = 4


def masked_softmax(vector, seq_lens):
    mask = vector.new(vector.size()).zero_()
    for i in range(seq_lens.size(0)):
        mask[i, :, :seq_lens[i]] = 1
    mask = Variable(mask, requires_grad=False)
    # mask = None
    if mask is None:
        result = torch.nn.functional.softmax(vector, dim=-1)
    else:
        result = torch.nn.functional.softmax(vector * mask, dim=-1)
        result = result * mask
        result = result / (result.sum(dim=-1, keepdim=True) + 1e-13)
    return result


class FuseNet(nn.Module):
    def __init__(self, config):
        super(FuseNet, self).__init__()
        self.linear = nn.Linear(config.hidden_size, config.hidden_size)
        self.linear2 = nn.Linear(2 * config.hidden_size, 2 * config.hidden_size)

    def forward(self, inputs):
        p, q = inputs
        lq = self.linear(q)
        lp = self.linear(p)
        mid = nn.Sigmoid()(lq + lp)
        output = p * mid + q * (1 - mid)
        return output


class SSingleMatchNet(nn.Module):
    def __init__(self, config):
        super(SSingleMatchNet, self).__init__()
        self.map_linear = nn.Linear(2 * config.hidden_size, 2 * config.hidden_size)
        self.trans_linear = nn.Linear(config.hidden_size, config.hidden_size)
        self.drop_module = nn.Dropout(2 * config.hidden_dropout_prob)
        self.rank_module = nn.Linear(config.hidden_size * 2, 1)

    def forward(self, inputs):
        proj_p, proj_q, seq_len = inputs
        trans_q = self.trans_linear(proj_q)
        att_weights = proj_p.bmm(torch.transpose(trans_q, 1, 2))
        att_norm = masked_softmax(att_weights, seq_len)

        att_vec = att_norm.bmm(proj_q)
        output = nn.ReLU()(self.trans_linear(att_vec))
        return output


def seperate_seq(sequence_output, doc_len, ques_len, option_len):
    doc_seq_output = sequence_output.new(sequence_output.size()).zero_()
    doc_ques_seq_output = sequence_output.new(sequence_output.size()).zero_()
    ques_seq_output = sequence_output.new(sequence_output.size()).zero_()
    ques_option_seq_output = sequence_output.new(sequence_output.size()).zero_()
    option_seq_output = sequence_output.new(sequence_output.size()).zero_()
    for i in range(doc_len.size(0)):
        doc_seq_output[i, :doc_len[i]] = sequence_output[i, 1:doc_len[i] + 1]
        doc_ques_seq_output[i, :doc_len[i] + ques_len[i]] = sequence_output[i, :doc_len[i] + ques_len[i]]
        ques_seq_output[i, :ques_len[i]] = sequence_output[i, doc_len[i] + 2:doc_len[i] + ques_len[i] + 2]
        ques_option_seq_output[i, :ques_len[i] + option_len[i]] = sequence_output[i,
                                                                  doc_len[i] + 1:doc_len[i] + ques_len[i] + option_len[
                                                                      i] + 1]
        option_seq_output[i, :option_len[i]] = sequence_output[i,
                                               doc_len[i] + ques_len[i] + 2:doc_len[i] + ques_len[i] + option_len[
                                                   i] + 2]
    return doc_ques_seq_output, ques_option_seq_output, doc_seq_output, ques_seq_output, option_seq_output


class BertForMultipleChoiceWithMatch(PreTrainedBertModel):

    def __init__(self, config, num_choices=2):
        super(BertForMultipleChoiceWithMatch, self).__init__(config)
        self.num_choices = num_choices
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, 1)
        self.classifier2 = nn.Linear(2 * config.hidden_size, 1)
        self.classifier3 = nn.Linear(3 * config.hidden_size, 1)
        self.classifier4 = nn.Linear(4 * config.hidden_size, 1)
        self.classifier6 = nn.Linear(6 * config.hidden_size, 1)
        self.ssmatch = SSingleMatchNet(config)
        self.pooler = BertPooler(config)
        self.fuse = FuseNet(config)
        self.apply(self.init_bert_weights)

    def forward(self, input_ids=None, token_type_ids=None, attention_mask=None, doc_len=None, ques_len=None,
                option_len=None, labels=None, is_3=False):
        flat_input_ids = input_ids.view(-1, input_ids.size(-1))
        doc_len = doc_len.view(-1, doc_len.size(0) * doc_len.size(1)).squeeze()
        ques_len = ques_len.view(-1, ques_len.size(0) * ques_len.size(1)).squeeze()
        option_len = option_len.view(-1, option_len.size(0) * option_len.size(1)).squeeze()

        flat_token_type_ids = token_type_ids.view(-1, token_type_ids.size(-1))
        flat_attention_mask = attention_mask.view(-1, attention_mask.size(-1))

        sequence_output, pooled_output = self.bert(flat_input_ids, flat_token_type_ids, flat_attention_mask,
                                                   output_all_encoded_layers=False)

        doc_ques_seq_output, ques_option_seq_output, doc_seq_output, ques_seq_output, option_seq_output = seperate_seq(
            sequence_output, doc_len, ques_len, option_len)

        pa_output = self.ssmatch([doc_seq_output, option_seq_output, option_len + 1])
        ap_output = self.ssmatch([option_seq_output, doc_seq_output, doc_len + 1])
        pq_output = self.ssmatch([doc_seq_output, ques_seq_output, ques_len + 1])
        qp_output = self.ssmatch([ques_seq_output, doc_seq_output, doc_len + 1])
        qa_output = self.ssmatch([ques_seq_output, option_seq_output, option_len + 1])
        aq_output = self.ssmatch([option_seq_output, ques_seq_output, ques_len + 1])

        pa_output_pool, _ = pa_output.max(1)
        ap_output_pool, _ = ap_output.max(1)
        pq_output_pool, _ = pq_output.max(1)
        qp_output_pool, _ = qp_output.max(1)
        qa_output_pool, _ = qa_output.max(1)
        aq_output_pool, _ = aq_output.max(1)

        pa_fuse = self.fuse([pa_output_pool, ap_output_pool])
        pq_fuse = self.fuse([pq_output_pool, qp_output_pool])
        qa_fuse = self.fuse([qa_output_pool, aq_output_pool])

        cat_pool = torch.cat([pa_fuse, pq_fuse, qa_fuse], 1)
        output_pool = self.dropout(cat_pool)
        match_logits = self.classifier3(output_pool)
        match_reshaped_logits = match_logits.view(-1, self.num_choices)

        if labels is not None:
            loss_fct = CrossEntropyLoss()
            match_loss = loss_fct(match_reshaped_logits, labels)
            return match_loss
        else:
            return match_reshaped_logits


class SwagExample(object):
    """A single training/test example for the SWAG dataset."""

    def __init__(self,
                 swag_id,
                 context_sentence,
                 start_ending,
                 ending_0,
                 ending_1,
                 ending_2,
                 ending_3,
                 label=None):
        self.swag_id = swag_id
        self.context_sentence = context_sentence
        self.start_ending = start_ending
        self.endings = [
            ending_0,
            ending_1,
            ending_2,
            ending_3,
        ]
        self.label = label

    def __str__(self):
        return self.__repr__()

    def __repr__(self):
        l = [
            f"swag_id: {self.swag_id}",
            f"context_sentence: {self.context_sentence}",
            f"start_ending: {self.start_ending}",
            f"ending_0: {self.endings[0]}",
            f"ending_1: {self.endings[1]}",
            f"ending_2: {self.endings[2]}",
            f"ending_3: {self.endings[3]}",
        ]

        if self.label is not None:
            l.append(f"label: {self.label}")

        return ", ".join(l)


class InputFeatures(object):
    def __init__(self,
                 example_id,
                 choices_features,
                 label

                 ):
        self.example_id = example_id
        self.choices_features = [
            {
                'input_ids': input_ids,
                'input_mask': input_mask,
                'segment_ids': segment_ids,
                'doc_len': doc_len,
                'ques_len': ques_len,
                'option_len': option_len
            }
            for _, input_ids, input_mask, segment_ids, doc_len, ques_len, option_len in choices_features
        ]
        self.label = label


def read_race(path):
    with open(path, 'r', encoding='utf_8') as f:
        data_all = json.load(f)
        article = []
        question = []
        st = []
        ct1 = []
        ct2 = []
        ct3 = []
        ct4 = []
        y = []
        q_id = []
        for instance in data_all:

            ct1.append(' '.join(instance['options'][0]))
            ct2.append(' '.join(instance['options'][1]))
            ct3.append(' '.join(instance['options'][2]))
            ct4.append(' '.join(instance['options'][3]))
            question.append(' '.join(instance['question']))
            # q_id.append(instance['q_id'])
            q_id.append(0)
            art = instance['article']
            l = []
            for i in art: l += i
            article.append(' '.join(l))
            # article.append(' '.join(instance['article']))
            y.append(instance['ground_truth'])
            '''
            ct1.append(instance['options'][0])
            ct2.append(instance['options'][1])
            ct3.append(instance['options'][2])
            ct4.append(instance['options'][3])
            question.append(instance['question'])
            q_id.append(0)
            article.append(instance['article'])
            y.append(instance['ground_truth'])
            '''
        return article, question, ct1, ct2, ct3, ct4, y, q_id


def read_swag_examples(input_file, is_training):
    # input_df = pd.read_json(input_file)
    if TASK_NAME == 'mctest':
        answer_file = input_file.replace('tsv', 'ans')
        article, question, ct1, ct2, ct3, ct4, y, q_id = parse_mc(input_file, answer_file)
    else:
        article, question, ct1, ct2, ct3, ct4, y, q_id = read_race(input_file)

    examples = [
        SwagExample(
            swag_id=s8,
            context_sentence=s1,
            start_ending=s2,  # in the swag dataset, the
            # common beginning of each
            # choice is stored in "sent2".
            ending_0=s3,
            ending_1=s4,
            ending_2=s5,
            ending_3=s6,
            label=s7 if is_training else None
        ) for i, (s1, s2, s3, s4, s5, s6, s7, s8), in enumerate(zip(article, question, ct1, ct2, ct3, ct4, y, q_id))
    ]

    return examples


def convert_examples_to_features(examples, tokenizer, max_seq_length,
                                 is_training):
    """Loads a data file into a list of `InputBatch`s."""

    # Swag is a multiple choice task. To perform this task using Bert,
    # we will use the formatting proposed in "Improving Language
    # Understanding by Generative Pre-Training" and suggested by
    # @jacobdevlin-google in this issue
    # https://github.com/google-research/bert/issues/38.
    #
    # Each choice will correspond to a sample on which we run the
    # inference. For a given Swag example, we will create the 4
    # following inputs:
    # - [CLS] context [SEP] choice_1 [SEP]
    # - [CLS] context [SEP] choice_2 [SEP]
    # - [CLS] context [SEP] choice_3 [SEP]
    # - [CLS] context [SEP] choice_4 [SEP]
    # The model will output a single value for each input. To get the
    # final decision of the model, we will run a softmax over these 4
    # outputs.
    features = []
    ool = 0
    for example_index, example in enumerate(examples):
        context_tokens = tokenizer.tokenize(example.context_sentence)
        start_ending_tokens = tokenizer.tokenize(example.start_ending)

        choices_features = []
        for ending_index, ending in enumerate(example.endings):
            # We create a copy of the context tokens in order to be
            # able to shrink it according to ending_tokens
            context_tokens_choice = context_tokens[:]  # + start_ending_tokens

            ending_token = tokenizer.tokenize(ending)
            option_len = len(ending_token)
            ques_len = len(start_ending_tokens)

            ending_tokens = start_ending_tokens + ending_token

            # Modifies `context_tokens_choice` and `ending_tokens` in
            # place so that the total length is less than the
            # specified length.  Account for [CLS], [SEP], [SEP] with
            # "- 3"
            # ending_tokens = start_ending_tokens + ending_tokens
            _truncate_seq_pair(context_tokens_choice, ending_tokens, max_seq_length - 3)
            doc_len = len(context_tokens_choice)
            if len(ending_tokens) + len(context_tokens_choice) >= max_seq_length - 3:
                ques_len = len(ending_tokens) - option_len

            tokens = ["[CLS]"] + context_tokens_choice + ["[SEP]"] + ending_tokens + ["[SEP]"]
            segment_ids = [0] * (len(context_tokens_choice) + 2) + [1] * (len(ending_tokens) + 1)

            input_ids = tokenizer.convert_tokens_to_ids(tokens)
            input_mask = [1] * len(input_ids)

            # Zero-pad up to the sequence length.
            padding = [0] * (max_seq_length - len(input_ids))
            input_ids += padding
            input_mask += padding
            segment_ids += padding

            assert len(input_ids) == max_seq_length
            assert len(input_mask) == max_seq_length
            assert len(segment_ids) == max_seq_length
            # assert (doc_len + ques_len + option_len) <= max_seq_length
            if (doc_len + ques_len + option_len) > max_seq_length:
                print(doc_len, ques_len, option_len, len(context_tokens_choice), len(ending_tokens))
                assert (doc_len + ques_len + option_len) <= max_seq_length
            choices_features.append((tokens, input_ids, input_mask, segment_ids, doc_len, ques_len, option_len))

        label = example.label
        if example_index < 5:
            logger.info("*** Example ***")
            logger.info(f"swag_id: {example.swag_id}")
            for choice_idx, (tokens, input_ids, input_mask, segment_ids, doc_len, ques_len, option_len) in enumerate(
                    choices_features):
                logger.info(f"choice: {choice_idx}")
                logger.info(f"tokens: {' '.join(tokens)}")
                logger.info(f"input_ids: {' '.join(map(str, input_ids))}")
                logger.info(f"input_mask: {' '.join(map(str, input_mask))}")
                logger.info(f"segment_ids: {' '.join(map(str, segment_ids))}")
            if is_training:
                logger.info(f"label: {label}")

        features.append(
            InputFeatures(
                example_id=example.swag_id,
                choices_features=choices_features,
                label=label
            )
        )

    return features


def _truncate_seq_pair(tokens_a, tokens_b, max_length):
    """Truncates a sequence pair in place to the maximum length."""

    # This is a simple heuristic which will always truncate the longer sequence
    # one token at a time. This makes more sense than truncating an equal percent
    # of tokens from each, since if one sequence is very short then each token
    # that's truncated likely contains more information than a longer sequence.
    pop_label = True
    while True:
        total_length = len(tokens_a) + len(tokens_b)
        if total_length <= max_length:
            break
        if len(tokens_a) > len(tokens_b):
            tokens_a.pop(1)
        else:
            tokens_b.pop(1)


def accuracy(out, labels):
    outputs = np.argmax(out, axis=1)
    return np.sum(outputs == labels)


def select_field(features, field):
    return [
        [
            choice[field]
            for choice in feature.choices_features
        ]
        for feature in features
    ]


def warmup_linear(x, warmup=0.002):
    if x < warmup:
        return x / warmup
    return 1.0 - x


def copy_optimizer_params_to_model(named_params_model, named_params_optimizer):
    """ Utility function for optimize_on_cpu and 16-bits training.
        Copy the parameters optimized on CPU/RAM back to the model on GPU
    """
    for (name_opti, param_opti), (name_model, param_model) in zip(named_params_optimizer, named_params_model):
        if name_opti != name_model:
            logger.error("name_opti != name_model: {} {}".format(name_opti, name_model))
            raise ValueError
        param_model.data.copy_(param_opti.data)


def set_optimizer_params_grad(named_params_optimizer, named_params_model, test_nan=False):
    """ Utility function for optimize_on_cpu and 16-bits training.
        Copy the gradient of the GPU parameters to the CPU/RAMM copy of the model
    """
    is_nan = False
    for (name_opti, param_opti), (name_model, param_model) in zip(named_params_optimizer, named_params_model):
        if name_opti != name_model:
            logger.error("name_opti != name_model: {} {}".format(name_opti, name_model))
            raise ValueError
        if param_model.grad is not None:
            if test_nan and torch.isnan(param_model.grad).sum() > 0:
                is_nan = True
            if param_opti.grad is None:
                param_opti.grad = torch.nn.Parameter(param_opti.data.new().resize_(*param_opti.data.size()))
            param_opti.grad.data.copy_(param_model.grad.data)
        else:
            param_opti.grad = None
    return is_nan


def main():
    parser = argparse.ArgumentParser()

    ## Required parameters
    parser.add_argument("--data_dir",
                        # default='data/openbookqa',
                        default='data/mc500',
                        # default='data/race',
                        type=str,
                        help="The input data dir. Should contain the .csv files (or other data files) for the task.")
    parser.add_argument("--bert_model",
                        default='bert-base-uncased',
                        type=str,
                        help="Bert pre-trained model selected in the list: bert-base-uncased, "
                             "bert-large-uncased, bert-base-cased, bert-base-multilingual, bert-base-chinese.")
    parser.add_argument("--do_lower_case",
                        default=True,
                        action='store_true',
                        help="Set this flag if you are using an uncased model.")
    parser.add_argument("--output_dir",
                        default='mctest_output',
                        # default='race_output',
                        type=str,
                        help="The output directory where the model checkpoints will be written.")
    parser.add_argument("--output_file",
                        default='output_test.txt',
                        type=str,
                        help="The output directory where the model checkpoints will be written.")
    parser.add_argument("--train_file",
                        # default='train_dev.json',
                        # default='test_dev.json',
                        default='mc500.train_dev.tsv',
                        type=str)
    parser.add_argument("--test_file",
                        # default='test.json',
                        # default='test_dev.json',
                        default='mc500.test.tsv',
                        type=str)
    parser.add_argument("--max_seq_length",
                        default=512,
                        # default=128,
                        type=int)
    parser.add_argument("--train_batch_size",
                        default=8,
                        # default=12,
                        type=int,
                        help="Total batch size for training.")
    parser.add_argument("--eval_batch_size",
                        default=8,
                        # default=12,
                        type=int,
                        help="Total batch size for eval.")
    parser.add_argument("--learning_rate",
                        default=1e-5,
                        type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument("--num_train_epochs",
                        default=5.0,
                        type=float,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--sa_file",
                        # default='sample_nsp_train_dev.json',
                        default='sample_train.json',
                        type=str)
    parser.add_argument("--test_middle",
                        default='testmiddle.json',
                        type=str)
    parser.add_argument("--test_high",
                        default='testhigh.json',
                        type=str)
    parser.add_argument("--model_name",
                        default='output_test.bin',
                        type=str,
                        help="The output directory where the model checkpoints will be written.")
    parser.add_argument("--mid_high",
                        default=False,
                        action='store_true',
                        help="Whether to run training.")
    parser.add_argument('--n_gpu',
                        type=int, default=4,
                        help='Loss scaling, positive power of 2 values can improve fp16 convergence.')
    parser.add_argument('--gradient_accumulation_steps',
                        type=int,
                        default=1,
                        help="Number of updates steps to accumulate before performing a backward/update pass.")
    parser.add_argument("--do_train",
                        default=True,
                        action='store_true',
                        help="Whether to run training.")
    parser.add_argument("--do_sa",
                        default=False,
                        action='store_true',
                        help="Whether to run training.")
    parser.add_argument("--do_eval",
                        default=True,
                        action='store_true',
                        help="Whether to run eval on the dev set.")
    parser.add_argument("--load_ft",
                        default=False,
                        action='store_true',
                        help="Set this flag if you are using an uncased model.")
    parser.add_argument("--warmup_proportion",
                        default=0.1,
                        type=float,
                        help="Proportion of training to perform linear learning rate warmup for. "
                             "E.g., 0.1 = 10%% of training.")
    parser.add_argument("--no_cuda",
                        default=False,
                        action='store_true',
                        help="Whether not to use CUDA when available")
    parser.add_argument("--local_rank",
                        type=int,
                        default=-1,
                        help="local_rank for distributed training on gpus")
    parser.add_argument('--seed',
                        type=int,
                        default=42,
                        help="random seed for initialization")
    parser.add_argument('--optimize_on_cpu',
                        default=False,
                        action='store_true',
                        help="Whether to perform optimization and keep the optimizer averages on CPU")
    parser.add_argument('--fp16',
                        default=False,
                        action='store_true',
                        help="Whether to use 16-bit float precision instead of 32-bit")
    parser.add_argument('--loss_scale',
                        type=float, default=4,
                        help='Loss scaling, positive power of 2 values can improve fp16 convergence.')

    args = parser.parse_args()
    logger.info(args)
    output_eval_file = os.path.join(args.output_dir, args.output_file)

    if os.path.exists(output_eval_file) and args.output_file != 'output_test.txt':
        raise ValueError("Output file ({}) already exists and is not empty.".format(output_eval_file))
    with open(output_eval_file, "w") as writer:
        writer.write("***** Eval results Epoch  %s *****\t\n" % (
            time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))))
        writer.write("%s\t\n" % args)

    args.model_name = args.output_file[:-4] + ".bin"
    # print(args.model_name)

    if args.local_rank == -1 or args.no_cuda:
        device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        n_gpu = args.n_gpu
    else:
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        n_gpu = args.n_gpu
        # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.distributed.init_process_group(backend='nccl')

    logger.info("device: {} n_gpu: {}, distributed training: {}, 16-bits training: {}".format(
        device, n_gpu, bool(args.local_rank != -1), args.fp16))

    if args.gradient_accumulation_steps < 1:
        raise ValueError("Invalid gradient_accumulation_steps parameter: {}, should be >= 1".format(
            args.gradient_accumulation_steps))

    args.train_batch_size = int(args.train_batch_size / args.gradient_accumulation_steps)

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)

    if not args.do_train and not args.do_eval:
        raise ValueError("At least one of `do_train` or `do_eval` must be True.")

    tokenizer = BertTokenizer.from_pretrained(args.bert_model, do_lower_case=args.do_lower_case)

    train_examples = None
    sa_examples = None
    num_train_steps = None
    num_sa_steps = None

    if args.do_sa:
        sa_examples = read_swag_examples(os.path.join(args.data_dir, args.sa_file), is_training=True)
        num_sa_steps = int(
            len(sa_examples) / args.train_batch_size / args.gradient_accumulation_steps)

    if args.do_train:
        train_examples = read_swag_examples(os.path.join(args.data_dir, args.train_file), is_training=True)
        num_train_steps = int(
            len(train_examples) / args.train_batch_size / args.gradient_accumulation_steps * args.num_train_epochs)

    # Prepare model
    if args.load_ft:
        # Load a trained model that you have fine-tuned
        logger.info("Load a trained model that you have fine-tuned")
        output_model_file = os.path.join(args.output_dir,
                                         'google_uncased_len440_batch4_lr5e6_epoch30_pqa_dual_full6_fuse_bilinear.bin')
        model_state_dict = torch.load(output_model_file)
        model = BertForMultipleChoiceWithMatch.from_pretrained(args.bert_model,
                                                               state_dict=model_state_dict,
                                                               num_choices=4)
    else:

        model = BertForMultipleChoiceWithMatch.from_pretrained(args.bert_model,
                                                               cache_dir=PYTORCH_PRETRAINED_BERT_CACHE / 'distributed_{}'.format(
                                                                   args.local_rank),
                                                               num_choices=4)

    if args.fp16:
        model.half()
    model.to(device)
    if args.local_rank != -1:
        try:
            from apex.parallel import DistributedDataParallel as DDP
        except ImportError:
            raise ImportError(
                "Please install apex from https://www.github.com/nvidia/apex to use distributed and fp16 training.")

        model = DDP(model)
    elif n_gpu > 1:
        model = torch.nn.DataParallel(model)

    param_optimizer = list(model.named_parameters())

    # hack to remove pooler, which is not used
    # thus it produce None grad that break apex
    param_optimizer = [n for n in param_optimizer if 'pooler' not in n[0]]

    no_decay = ['bias', 'gamma', 'beta']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay_rate': 0.01},
        {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay_rate': 0.0}
    ]

    t_total = num_train_steps
    if args.local_rank != -1:
        t_total = t_total // torch.distributed.get_world_size()

    if args.fp16:
        try:
            from apex.optimizers import FP16_Optimizer
            from apex.optimizers import FusedAdam
        except ImportError:
            raise ImportError(
                "Please install apex from https://www.github.com/nvidia/apex to use distributed and fp16 training.")

        optimizer = FusedAdam(optimizer_grouped_parameters,
                              lr=args.learning_rate,
                              bias_correction=False,
                              max_grad_norm=1.0)
        if args.loss_scale == 0:
            optimizer = FP16_Optimizer(optimizer, dynamic_loss_scale=True)
        else:
            optimizer = FP16_Optimizer(optimizer, static_loss_scale=args.loss_scale)
    else:
        optimizer = BertAdam(optimizer_grouped_parameters,
                             lr=args.learning_rate,
                             warmup=args.warmup_proportion,
                             t_total=t_total)

    global_step = 0
    best_accuracy = 0

    if args.load_ft:
        if args.do_eval and (args.local_rank == -1 or torch.distributed.get_rank() == 0):
            eval_examples = read_swag_examples(os.path.join(args.data_dir, args.test_file), is_training=True)
            eval_features = convert_examples_to_features(
                eval_examples, tokenizer, args.max_seq_length, True)
            logger.info("***** Running evaluation *****")
            logger.info("  Num examples = %d", len(eval_examples))
            logger.info("  Batch size = %d", args.eval_batch_size)
            all_input_ids = torch.tensor(select_field(eval_features, 'input_ids'), dtype=torch.long)
            all_input_mask = torch.tensor(select_field(eval_features, 'input_mask'), dtype=torch.long)
            all_segment_ids = torch.tensor(select_field(eval_features, 'segment_ids'), dtype=torch.long)
            all_doc_len = torch.tensor(select_field(eval_features, 'doc_len'), dtype=torch.long)
            all_ques_len = torch.tensor(select_field(eval_features, 'ques_len'), dtype=torch.long)
            all_option_len = torch.tensor(select_field(eval_features, 'option_len'), dtype=torch.long)
            all_label = torch.tensor([f.label for f in eval_features], dtype=torch.long)
            eval_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_label, all_doc_len,
                                      all_ques_len, all_option_len)
            # Run prediction for full data
            eval_sampler = SequentialSampler(eval_data)
            eval_dataloader = DataLoader(eval_data, sampler=eval_sampler, batch_size=args.eval_batch_size)

            model.eval()
            eval_loss, eval_accuracy = 0, 0
            nb_eval_steps, nb_eval_examples = 0, 0
            for input_ids, input_mask, segment_ids, label_ids, all_doc_len, all_ques_len, all_option_len in tqdm(
                    eval_dataloader, desc="Iteration"):
                input_ids = input_ids.to(device)
                input_mask = input_mask.to(device)
                segment_ids = segment_ids.to(device)
                all_doc_len = all_doc_len.to(device)
                all_ques_len = all_ques_len.to(device)
                all_option_len = all_option_len.to(device)
                label_ids = label_ids.to(device)

                with torch.no_grad():
                    tmp_eval_loss = model(input_ids, segment_ids, input_mask, all_doc_len, all_ques_len, all_option_len)
                    logits = model(input_ids, segment_ids, input_mask, all_doc_len, all_ques_len, all_option_len)

                logits = logits.detach().cpu().numpy()
                label_ids = label_ids.to('cpu').numpy()
                tmp_eval_accuracy = accuracy(logits, label_ids)

                eval_loss += tmp_eval_loss.mean().item()
                eval_accuracy += tmp_eval_accuracy

                nb_eval_examples += input_ids.size(0)
                nb_eval_steps += 1

            eval_loss = eval_loss / nb_eval_steps
            eval_accuracy = eval_accuracy / nb_eval_examples

            best_accuracy = eval_accuracy
            result = {'eval_loss': eval_loss,
                      'eval_accuracy': eval_accuracy,
                      'global_step': global_step}
            output_eval_file = os.path.join(args.output_dir, args.output_file)
            with open(output_eval_file, "a") as writer:
                for key in sorted(result.keys()):
                    logger.info("  %s = %s", key, str(result[key]))
                    writer.write("%s = %s\t" % (key, str(result[key])))
                writer.write("\t\n")

    if args.do_train:
        train_features = convert_examples_to_features(
            train_examples, tokenizer, args.max_seq_length, True)
        logger.info("***** Running training *****")
        logger.info("  Num examples = %d", len(train_examples))
        logger.info("  Batch size = %d", args.train_batch_size)
        logger.info("  Num steps = %d", num_train_steps)
        with open(output_eval_file, "a") as writer:
            writer.write("***** Running training *****\t\n")
            writer.write("  Num examples = %d\t\n" % len(train_examples))
            writer.write("  Batch size = %d\t\n" % args.train_batch_size)
            writer.write("  Num steps = %d\t\n" % num_train_steps)

        all_input_ids = torch.tensor(select_field(train_features, 'input_ids'), dtype=torch.long)
        all_input_mask = torch.tensor(select_field(train_features, 'input_mask'), dtype=torch.long)
        all_segment_ids = torch.tensor(select_field(train_features, 'segment_ids'), dtype=torch.long)
        all_doc_len = torch.tensor(select_field(train_features, 'doc_len'), dtype=torch.long)
        all_ques_len = torch.tensor(select_field(train_features, 'ques_len'), dtype=torch.long)
        all_option_len = torch.tensor(select_field(train_features, 'option_len'), dtype=torch.long)

        all_label = torch.tensor([f.label for f in train_features], dtype=torch.long)
        train_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_label, all_doc_len, all_ques_len,
                                   all_option_len)
        if args.local_rank == -1:
            train_sampler = RandomSampler(train_data)
        else:
            train_sampler = DistributedSampler(train_data)
        train_dataloader = DataLoader(train_data, sampler=train_sampler, batch_size=args.train_batch_size)

        for epoch in trange(int(args.num_train_epochs), desc="Epoch"):
            model.train()
            tr_loss = 0
            nb_tr_examples, nb_tr_steps = 0, 0
            for step, batch in enumerate(tqdm(train_dataloader, desc="Iteration")):
                batch = tuple(t.to(device) for t in batch)
                input_ids, input_mask, segment_ids, label_ids, doc_len, ques_len, option_len = batch
                loss = model(input_ids, segment_ids, input_mask, doc_len, ques_len, option_len, label_ids)
                if n_gpu > 1:
                    loss = loss.mean()  # mean() to average on multi-gpu.
                if args.fp16 and args.loss_scale != 1.0:
                    # rescale loss for fp16 training
                    # see https://docs.nvidia.com/deeplearning/sdk/mixed-precision-training/index.html
                    loss = loss * args.loss_scale
                if args.gradient_accumulation_steps > 1:
                    loss = loss / args.gradient_accumulation_steps
                tr_loss += loss.item()
                nb_tr_examples += input_ids.size(0)
                nb_tr_steps += 1

                if args.fp16:
                    optimizer.backward(loss)
                else:
                    loss.backward()
                if (step + 1) % args.gradient_accumulation_steps == 0:
                    # modify learning rate with special warm up BERT uses
                    lr_this_step = args.learning_rate * warmup_linear(global_step / t_total, args.warmup_proportion)
                    for param_group in optimizer.param_groups:
                        param_group['lr'] = lr_this_step
                    optimizer.step()
                    optimizer.zero_grad()
                    global_step += 1

            # logger.info("lr = %f", lr_this_step)
            lr_pre = lr_this_step
            if args.do_eval and (args.local_rank == -1 or torch.distributed.get_rank() == 0):
                eval_examples = read_swag_examples(os.path.join(args.data_dir, args.test_file), is_training=True)
                eval_features = convert_examples_to_features(
                    eval_examples, tokenizer, args.max_seq_length, True)
                logger.info("***** Running evaluation *****")
                logger.info("  Num examples = %d", len(eval_examples))
                logger.info("  Batch size = %d", args.eval_batch_size)
                all_input_ids = torch.tensor(select_field(eval_features, 'input_ids'), dtype=torch.long)
                all_input_mask = torch.tensor(select_field(eval_features, 'input_mask'), dtype=torch.long)
                all_segment_ids = torch.tensor(select_field(eval_features, 'segment_ids'), dtype=torch.long)
                all_doc_len = torch.tensor(select_field(eval_features, 'doc_len'), dtype=torch.long)
                all_ques_len = torch.tensor(select_field(eval_features, 'ques_len'), dtype=torch.long)
                all_option_len = torch.tensor(select_field(eval_features, 'option_len'), dtype=torch.long)
                all_label = torch.tensor([f.label for f in eval_features], dtype=torch.long)
                eval_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_label, all_doc_len,
                                          all_ques_len, all_option_len)
                # Run prediction for full data
                eval_sampler = SequentialSampler(eval_data)
                eval_dataloader = DataLoader(eval_data, sampler=eval_sampler, batch_size=args.eval_batch_size)

                model.eval()
                eval_loss, eval_accuracy = 0, 0
                nb_eval_steps, nb_eval_examples = 0, 0

                for input_ids, input_mask, segment_ids, label_ids, all_doc_len, all_ques_len, all_option_len in tqdm(
                        eval_dataloader, desc="Evaluating"):
                    input_ids = input_ids.to(device)
                    input_mask = input_mask.to(device)
                    segment_ids = segment_ids.to(device)
                    all_doc_len = all_doc_len.to(device)
                    all_ques_len = all_ques_len.to(device)
                    all_option_len = all_option_len.to(device)
                    label_ids = label_ids.to(device)

                    with torch.no_grad():
                        tmp_eval_loss = model(input_ids, segment_ids, input_mask, all_doc_len, all_ques_len,
                                              all_option_len, label_ids)
                        logits = model(input_ids, segment_ids, input_mask, all_doc_len, all_ques_len, all_option_len)

                    logits = logits.detach().cpu().numpy()
                    label_ids = label_ids.to('cpu').numpy()
                    tmp_eval_accuracy = accuracy(logits, label_ids)

                    eval_loss += tmp_eval_loss.mean().item()
                    eval_accuracy += tmp_eval_accuracy

                    nb_eval_examples += input_ids.size(0)
                    nb_eval_steps += 1

                eval_loss = eval_loss / nb_eval_steps
                eval_accuracy = eval_accuracy / nb_eval_examples

                if eval_accuracy > best_accuracy:
                    logger.info("**** Saving model.... *****")
                    best_accuracy = eval_accuracy
                    model_to_save = model.module if hasattr(model, 'module') else model  # Only save the model it-self
                    output_model_file = os.path.join(args.output_dir, args.model_name)
                    torch.save(model_to_save.state_dict(), output_model_file)

                result = {'eval_loss': eval_loss,
                          'best_accuracy': best_accuracy,
                          'eval_accuracy': eval_accuracy,
                          # 'global_step': global_step,
                          'lr_now': lr_pre,
                          'loss': tr_loss / nb_tr_steps}
                output_eval_file = os.path.join(args.output_dir, args.output_file)
                with open(output_eval_file, "a") as writer:
                    logger.info("***** Eval results *****")
                    writer.write("\t\n***** Eval results Epoch %d  %s *****\t\n" % (
                        epoch, time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))))
                    for key in sorted(result.keys()):
                        logger.info("  %s = %s", key, str(result[key]))
                        writer.write("%s = %s\t" % (key, str(result[key])))
                    writer.write("\t\n")


if __name__ == "__main__":
    main()