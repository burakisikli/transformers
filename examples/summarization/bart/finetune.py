import argparse
import glob
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Tuple, Union

import numpy as np
import torch
import time
import torch.nn.functional as F
from rouge_score import rouge_scorer, scoring
from torch import nn
from torch.utils.data import DataLoader

from lightning_base import BaseTransformer, add_generic_args, generic_train, get_linear_schedule_with_warmup
from transformers import BartConfig, BartForConditionalGeneration, BartTokenizer
from transformers.modeling_bart import invert_mask


try:
    from .utils import SummarizationDataset
    from .bart_distiller import copy_decoder_layers, init_student
except ImportError:
    from utils import SummarizationDataset
    from bart_distiller import copy_decoder_layers, init_student

logger = logging.getLogger(__name__)

ROUGE_KEYS = ["rouge1", "rouge2", "rougeL"]

from pytorch_lightning.loggers import WandbLogger
from durbango import pickle_save
import pandas as pd

def calculate_rouge(output_lns: List[str], reference_lns: List[str]) -> Dict:
    # score_file = Path(score_path).open("w")
    scorer = rouge_scorer.RougeScorer(ROUGE_KEYS, use_stemmer=True)
    aggregator = scoring.BootstrapAggregator()

    for reference_ln, output_ln in zip(reference_lns, output_lns):
        scores = scorer.score(reference_ln, output_ln)
        aggregator.add_scores(scores)

    result = aggregator.aggregate()
    return {k: v.mid.fmeasure for k, v in result.items()}


class SummarizationTrainer(BaseTransformer):
    mode = "language-modeling"
    loss_names = ["loss", "ce_loss", "mlm_loss"]
    def __init__(self, hparams, **kwargs):
        super().__init__(hparams, num_labels=None, mode=self.mode, **kwargs)
        self.dataset_kwargs: dict = dict(
            data_dir=self.hparams.data_dir,
            max_source_length=self.hparams.max_source_length,
            max_target_length=self.hparams.max_target_length,
        )
        self.model: BartForConditionalGeneration
        self.metrics = {'train': [], 'val': [], 'test': []}
        self.output_dir = Path(self.hparams.output_dir)
        self.metrics_save_path = Path(self.output_dir) /'metrics.pkl'
        self.dataset_kwargs: dict = dict(
            data_dir=self.hparams.data_dir,
            max_source_length=self.hparams.max_source_length,
            max_target_length=self.hparams.max_target_length,
            overwrite_cache=self.hparams.no_cache,
        )

    @property
    def metrics_df(self):
        return pd.DataFrame(self.metrics)

    def forward(self, input_ids, attention_mask=None, decoder_input_ids=None, lm_labels=None):
        return self.model(
            input_ids, attention_mask=attention_mask, decoder_input_ids=decoder_input_ids, lm_labels=lm_labels,
        )

    def _step(self, batch):
        pad_token_id = self.tokenizer.pad_token_id
        source_ids, source_mask, y = batch["source_ids"], batch["source_mask"], batch["target_ids"]
        y_ids = y[:, :-1].contiguous()
        lm_labels = y[:, 1:].clone()
        lm_labels[y[:, 1:] == pad_token_id] = -100
        outputs = self(source_ids, attention_mask=source_mask, decoder_input_ids=y_ids, lm_labels=lm_labels,)

        loss = outputs[0]

        return loss,

    def training_step(self, batch, batch_idx):
        loss_tensors = self._step(batch)
        tensorboard_logs = {name: loss for name, loss in zip(self.loss_names, loss_tensors)}
        return {"loss": loss_tensors[0], "log": tensorboard_logs}

    def validation_step(self, batch, batch_idx):
        return self._generative_step(batch)

    def validation_end(self, outputs, prefix="val"):
        losses = {k: torch.stack([x[k] for x in outputs]).mean().item() for k in self.loss_names}
        rouges = {k: np.array([x[k] for x in outputs]).mean() for k in ROUGE_KEYS + ['gen_time']}
        losses.update(rouges)
        metrics = {f"{prefix}_{k}": x for k, x in losses.items()}
        self.metrics[prefix].append(metrics)
        pickle_save(self.metrics, self.metrics_save_path)
        return {"log": metrics, **losses}  # Where does this go!

    def _generative_step(self, batch):
        pad_token_id = self.tokenizer.pad_token_id
        source_ids, source_mask, y = SummarizationDataset.trim_seq2seq_batch(batch, pad_token_id)
        # NOTE: the following kwargs get more speed and lower quality summaries than those in evaluate_cnn.py
        t0 = time.time()
        generated_ids = self.model.generate(input_ids=source_ids, attention_mask=source_mask, use_cache=True,)
        gen_time = time.time() - t0
        preds = self.tokenizer.batch_decode(generated_ids, skip_special_tokens=True, clean_up_tokenization_spaces=True)
        target = self.tokenizer.batch_decode(y, skip_special_tokens=True, clean_up_tokenization_spaces=True)
        loss_tensors = self._step(batch)
        base_metrics = {name: loss for name, loss in zip(self.loss_names, loss_tensors)}
        rouge: Dict = calculate_rouge(preds, target)
        base_metrics.update(gen_time=gen_time, **rouge)
        return base_metrics

    def test_step(self, batch, batch_idx):
        return self._generative_step(batch)

    def test_end(self, outputs):
        return self.validation_end(outputs, prefix="test")

    def test_epoch_end(self, outputs):
        output_test_predictions_file = os.path.join(self.hparams.output_dir, "test_predictions.txt")
        output_test_targets_file = os.path.join(self.hparams.output_dir, "test_targets.txt")
        # write predictions and targets for later rouge evaluation.
        with open(output_test_predictions_file, "w+") as p_writer, open(output_test_targets_file, "w+") as t_writer:
            for output_batch in outputs:
                p_writer.writelines(s + "\n" for s in output_batch["preds"])
                t_writer.writelines(s + "\n" for s in output_batch["target"])
            p_writer.close()
            t_writer.close()

        return self.test_end(outputs)

    def get_dataloader(self, type_path: str, batch_size: int, shuffle: bool = False) -> DataLoader:
        dataset = SummarizationDataset(self.tokenizer, type_path=type_path, **self.dataset_kwargs)
        dataloader = DataLoader(
            dataset, batch_size=batch_size, collate_fn=dataset.collate_fn, shuffle=shuffle, num_workers=4
        )
        return dataloader

    def train_dataloader(self) -> DataLoader:
        dataloader = self.get_dataloader("train", batch_size=self.hparams.train_batch_size, shuffle=True)
        t_total = (
            (len(dataloader.dataset) // (self.hparams.train_batch_size * max(1, self.hparams.n_gpu)))
            // self.hparams.gradient_accumulation_steps
            * float(self.hparams.num_train_epochs)
        )
        scheduler = get_linear_schedule_with_warmup(
            self.opt, num_warmup_steps=self.hparams.warmup_steps, num_training_steps=t_total
        )
        self.lr_scheduler = scheduler
        return dataloader

    def val_dataloader(self) -> DataLoader:
        return self.get_dataloader("val", batch_size=self.hparams.eval_batch_size)

    def test_dataloader(self) -> DataLoader:
        return self.get_dataloader("test", batch_size=self.hparams.eval_batch_size)

    @staticmethod
    def add_model_specific_args(parser, root_dir):
        BaseTransformer.add_model_specific_args(parser, root_dir)
        # Add BART specific options
        parser.add_argument(
            "--max_source_length",
            default=1024,
            type=int,
            help="The maximum total input sequence length after tokenization. Sequences longer "
            "than this will be truncated, sequences shorter will be padded.",
        )
        parser.add_argument(
            "--max_target_length",
            default=56,
            type=int,
            help="The maximum total input sequence length after tokenization. Sequences longer "
            "than this will be truncated, sequences shorter will be padded.",
        )

        parser.add_argument(
            "--data_dir",
            default=None,
            type=str,
            required=True,
            help="The input data dir. Should contain the dataset files for the CNN/DM summarization task.",
        )
        parser.add_argument(
            "--no_cache", action="store_true",
        )

        return parser


def freeze_part(model: nn.Module):
    for par in model.parameters():
        par.requires_grad = False


class SummarizationDistiller(SummarizationTrainer):
    def __init__(self, hparams):

        # Dump empty student model at a path, then call from_pretrained on it
        teacher = BartForConditionalGeneration.from_pretrained(hparams.teacher).eval()
        student_updates = {"decoder_layers": teacher.config.decoder_layers // 2}
        if student_updates["decoder_layers"] == 6:
            layers_to_copy = [0, 2, 4, 7, 9, 11]
        else:
            layers_to_copy = list(range(teacher.config.decoder_layers))[::2]
        kw = teacher.config.to_diff_dict()
        kw.update(student_updates)
        student_cfg = BartConfig(**kw)
        student_model = BartForConditionalGeneration(student_cfg)
        student_model, info = init_student(student_model, teacher)
        copy_decoder_layers(teacher, student_model, l2copy=layers_to_copy)
        Path(hparams.model_name_or_path).mkdir(exist_ok=True)
        student_model.save_pretrained(hparams.model_name_or_path)
        tokenizer = BartTokenizer.from_pretrained("bart-large")
        super().__init__(hparams, model=student_model, config=student_cfg, tokenizer=tokenizer)

        assert len(self.model.model.decoder.layers) == len(layers_to_copy)
        self.model.teacher = teacher
        # self.teacher = teacher
        self.ce_loss_fct = nn.KLDivLoss(reduction="batchmean")
        self.freeze_stuff()
        self.temperature = 2.0
        self.alpha_mlm = hparams.alpha_mlm
        self.alpha_ce = hparams.alpha_ce

    def freeze_stuff(self):
        freeze_part(self.model.model.encoder)
        freeze_part(self.model.teacher)
        freeze_part(self.model.model.shared)
        d = self.model.model.decoder
        freeze_part(d.embed_positions)
        freeze_part(d.embed_tokens)

    def _step(self, batch):
        pad_token_id = self.tokenizer.pad_token_id
        source_ids, source_mask, y = batch["input_ids"], batch["attention_mask"], batch["decoder_input_ids"]
        y_ids = y[:, :-1].contiguous()
        lm_labels = y[:, 1:].clone()
        lm_labels[y[:, 1:] == pad_token_id] = -100
        # noinspection PyCallingNonCallable
        sloss, slogits, *trash = self(
            source_ids, attention_mask=source_mask, decoder_input_ids=y_ids, lm_labels=lm_labels,
        )
        with torch.no_grad():
            tloss, tlogits, *trash = self.model.teacher(
                source_ids, attention_mask=source_mask, decoder_input_ids=y_ids, lm_labels=lm_labels,
            )
        loss_ce, s_logits_slct, t_logits_slct = self.calc_ce_loss(self.model.model.last_padding_mask, slogits, tlogits)
        blended_loss = loss_ce * self.alpha_ce + self.alpha_mlm * sloss
        return blended_loss, loss_ce, sloss

    def calc_ce_loss(self, mask, s_logits, t_logits):
        if mask is not None:
            # mask has True at padding_idx
            mask = invert_mask(mask)
            sel_mask = mask[:, :, None].expand_as(s_logits)
            s_logits_slct = torch.masked_select(
                s_logits, sel_mask
            )  # (bs * seq_length * voc_size) modulo the 1s in mask
            t_logits_slct = torch.masked_select(
                t_logits, sel_mask
            )  # (bs * seq_length * voc_size) modulo the 1s in mask
        else:

            t_logits_slct = t_logits
            s_logits_slct = s_logits  # (bs * seq_length * voc_size) modulo the 1s in mask
        s_logits_slct = s_logits_slct.view(-1, s_logits.size(-1))  # (bs * seq_length, voc_size) modulo the 1s in mask
        t_logits_slct = t_logits_slct.view(-1, s_logits.size(-1))  # (bs * seq_length, voc_size) modulo the 1s in mask
        assert t_logits_slct.size() == s_logits_slct.size()
        loss_ce = (
            self.ce_loss_fct(
                F.log_softmax(s_logits_slct / self.temperature, dim=-1),
                F.softmax(t_logits_slct / self.temperature, dim=-1),
            )
            * (self.temperature) ** 2
        )
        return loss_ce, s_logits_slct, t_logits_slct

    def calc_cos_loss(self, attention_mask, s_hidden_states, t_hidden_states):
        s_hidden_states = s_hidden_states[-1]  # (bs, seq_length, dim)
        t_hidden_states = t_hidden_states[-1]  # (bs, seq_length, dim)
        mask = attention_mask.unsqueeze(-1).expand_as(s_hidden_states)  # (bs, seq_length, dim)
        assert s_hidden_states.size() == t_hidden_states.size()
        dim = s_hidden_states.size(-1)
        s_hidden_states_slct = torch.masked_select(s_hidden_states, mask)  # (bs * seq_length * dim)
        s_hidden_states_slct = s_hidden_states_slct.view(-1, dim)  # (bs * seq_length, dim)
        t_hidden_states_slct = torch.masked_select(t_hidden_states, mask)  # (bs * seq_length * dim)
        t_hidden_states_slct = t_hidden_states_slct.view(-1, dim)  # (bs * seq_length, dim)
        target = s_hidden_states_slct.new(s_hidden_states_slct.size(0)).fill_(1)  # (bs * seq_length,)
        loss_cos = self.cosine_loss_fct(s_hidden_states_slct, t_hidden_states_slct, target)
        return loss_cos

    @staticmethod
    def add_model_specific_args(parser, root_dir):
        SummarizationTrainer.add_model_specific_args(parser, root_dir)
        parser.add_argument(
            "--teacher", default=None, type=str, required=True,
        )
        parser.add_argument(
            "--alpha_ce", default=0.8, type=float,
        )
        parser.add_argument(
            "--alpha_mlm", default=0.2, type=float,
        )

        return parser


def main(args):
    # If output_dir not provided, a folder will be generated in pwd
    if not args.output_dir:
        args.output_dir = os.path.join("./results", f"{args.task}_{time.strftime('%Y%m%d_%H%M%S')}",)
        os.makedirs(args.output_dir)
    model: pl.LightningModule = SummarizationTrainer(args)
    trainer: pl.Trainer = generic_train(model, args)
    checkpoints = list(sorted(glob.glob(os.path.join(args.output_dir, "checkpointepoch=*.ckpt"), recursive=True)))
    model = model.load_from_checkpoint(checkpoints[-1])
    trainer.test(model)

import pytorch_lightning as pl

def run_distiller(args):
    if not args.output_dir:
        args.output_dir = os.path.join("./results", f"{args.task}_{time.strftime('%Y%m%d_%H%M%S')}",)
        os.makedirs(args.output_dir)
    model: pl.LightningModule = SummarizationDistiller(args)
    trainer: pl.Trainer = generic_train(model, args, early_stopping_callback=True)
    checkpoints = list(sorted(glob.glob(os.path.join(args.output_dir, "checkpointepoch=*.ckpt"), recursive=True)))
    # model = model.load_from_checkpoint(checkpoints[-1])
    trainer.test()
    #model.metrics_df.to_csv(Path(model.output_dir)/'metrics.csv')


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    add_generic_args(parser, os.getcwd())
    parser = SummarizationDistiller.add_model_specific_args(parser, os.getcwd())
    args = parser.parse_args()

    run_distiller(args)
