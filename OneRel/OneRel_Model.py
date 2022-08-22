from lib2to3.pgen2 import token
from pyparsing import line
import torch.nn as nn
from transformers.models.bert.modeling_bert import BertModel
from transformers.models.bert.tokenization_bert_fast import BertTokenizerFast
import torch
import pytorch_lightning as pl
import numpy as np
import json
import os
from torch.utils.data import Dataset
from tqdm import tqdm
from utils.utils import rematch,find_head_idx
from loss_func import MultiCEFocalLoss

TAG2ID = {
    "A": 0,
    "HB-TB": 1,
    "HB-TE": 2,
    "HE-TE": 3
}


class OneRelModel(nn.Module):
    def __init__(self, config):
        super(OneRelModel, self).__init__()
        self.config = config
        self.bert = BertModel.from_pretrained(
            config.pretrain_path, cache_dir='./bertbaseuncased')
        self.bert_dim = self.bert.config.hidden_size
        self.relation_matrix = nn.Linear(
            self.bert_dim * 3, self.config.relation_number * self.config.tag_size)
        self.projection_matrix = nn.Linear(
            self.bert_dim * 2, self.bert_dim * 3)

        self.dropout = nn.Dropout(self.config.dropout_prob)
        self.dropout_2 = nn.Dropout(self.config.entity_pair_dropout)
        self.activation = nn.ReLU()

    def forward(self, input_ids, attention_mask):
        # [batch_size, seq_len, bert_dim(768)]
        encoded_text = self.bert(
            input_ids, attention_mask=attention_mask)[0]
        encoded_text = self.dropout(encoded_text)
        # encoded_text: [batch_size, seq_len, bert_dim(768)] 1,2,3
        batch_size, seq_len, bert_dim = encoded_text.size()
        # head: [batch_size, seq_len * seq_len, bert_dim(768)] 1,1,1, 2,2,2, 3,3,3
        head_representation = encoded_text.unsqueeze(2).expand(
            batch_size, seq_len, seq_len, bert_dim).reshape(batch_size, seq_len*seq_len, bert_dim)
        # tail: [batch_size, seq_len * seq_len, bert_dim(768)] 1,2,3, 1,2,3, 1,2,3
        tail_representation = encoded_text.repeat(1, seq_len, 1)
        # [batch_size, seq_len * seq_len, bert_dim(768)*2]
        entity_pairs = torch.cat(
            [head_representation, tail_representation], dim=-1)

        # [batch_size, seq_len * seq_len, bert_dim(768)*3]
        entity_pairs = self.projection_matrix(entity_pairs)
        entity_pairs = self.dropout_2(entity_pairs)
        entity_pairs = self.activation(entity_pairs)

        # [batch_size, seq_len * seq_len, rel_num * tag_size] -> [batch_size, seq_len, seq_len, rel_num, tag_size]
        output = self.relation_matrix(entity_pairs).reshape(
            batch_size, seq_len, seq_len, self.config.relation_number, self.config.tag_size)
        return output


class OneRelPytochLighting(pl.LightningModule):
    def __init__(self, args) -> None:
        super().__init__()
        self.args = args
        self.model = OneRelModel(args)
        self.save_hyperparameters(args)
        self.loss = nn.CrossEntropyLoss(reduction="none")
        self.focal_loss = MultiCEFocalLoss(self.args.tag_size)
        with open(args.relation, 'r') as f:
            relation = json.load(f)
        self.id2rel = relation[0]
        self.epoch = 0

    def forward(self, *args, **kwargs):
        return super().forward(*args, **kwargs)

    def training_step(self, batches, batch_idx):
        batch_token_ids = batches['token_ids']
        batch_attention_masks = batches['mask']
        batch_loss_masks = batches['loss_mask']
        triple_matrix = batches['triple_matrix']
        # [batch_size, seq_len, seq_len, rel_num, tag_size]
        outputs = self.model(batch_token_ids, batch_attention_masks)
        # [batch_size, tag_size, rel_num, seq_len, seq_len]
        outputs = outputs.permute(0, 4, 3, 1, 2)
        loss = self.loss(outputs, triple_matrix)
        loss = torch.sum(loss * batch_loss_masks) / torch.sum(batch_loss_masks)
        focal_loss = self.focal_loss(outputs, triple_matrix)
        focal_loss = torch.sum(focal_loss * batch_loss_masks) / torch.sum(batch_loss_masks)
        return loss+focal_loss

    def validation_step(self, batches, batch_idx):
        batch_token_ids = batches['token_ids']
        batch_attention_masks = batches['mask']
        batch_loss_masks = batches['loss_mask']
        triple_matrix = batches['triple_matrix']
        triples = batches['triples']
        tokens = batches['tokens']
        texts = batches['texts']
        offset_maps = batches['offset_map']
        # [batch_size, seq_len, seq_len, rel_num, tag_size]
        pred_triple_matrix = self.model(batch_token_ids, batch_attention_masks)
        # [batch_size, seq_len, seq_len, rel_num]->[batch_size, rel_num, seq_len, seq_len]
        pred_triple_matrix = pred_triple_matrix.argmax(
            dim=-1).permute(0, 3, 1, 2)
        pred_triples = self.parse_prediction(
            pred_triple_matrix, batch_loss_masks, texts,offset_maps,triples)
        return pred_triples, triples, texts

    def parse_prediction(self, pred_triple_matrix, batch_loss_masks, texts,offset_maps,triples):
        batch_size, rel_numbers, seq_lens, seq_lens = pred_triple_matrix.shape
        batch_triple_list = []
        for batch in range(batch_size):
            mapping = rematch(offset_maps[batch])
            triple= triples[batch]
            triple_matrix = pred_triple_matrix[batch].cpu().numpy()
            masks = batch_loss_masks[batch].cpu().numpy()
            triple_matrix = triple_matrix*masks
            text = texts[batch]
            triple_list = []
            for r_index in range(rel_numbers):
                rel_triple_matrix = triple_matrix[r_index]
                heads, tails = np.where(rel_triple_matrix > 0)
                pair_numbers = len(heads)
                rel_triple = rel_triple_matrix[(heads, tails)]
                # if pair_numbers>0:
                #     print(r_index,heads, tails,rel_triple)
                rel = self.id2rel[str(int(r_index))]
                for i in range(pair_numbers):
                    h_start_index = heads[i]
                    t_start_index = tails[i]
                    # 如果当前第一个标签为HB-TB,即subject begin，object begin
                    if rel_triple_matrix[h_start_index][t_start_index] == TAG2ID['HB-TB']:
                        # 如果下一个标签为HB-TE,即subject begin，object end
                        find_hb_te= False
                        if i+1 < pair_numbers:
                            t_end_index = tails[i+1]
                            if rel_triple_matrix[h_start_index][t_end_index] == TAG2ID['HB-TE']:
                                # 那么就向下找
                                find_hb_te = True
                                find_he_te = False
                                for h_end_index in range(h_start_index, seq_lens):
                                    # 向下找到了结尾位置,即subject end，object end
                                    if rel_triple_matrix[h_end_index][t_end_index] == TAG2ID['HE-TE']:
                                        sub = self.decode_entity(text, mapping, h_start_index, h_end_index)
                                        obj = self.decode_entity(text, mapping, t_start_index, t_end_index)

                                        if len(sub) > 0 and len(obj) > 0:
                                            triple_list.append((sub, rel, obj))
                                            find_he_te = True
                                        # break
                                if not find_he_te:
                                    # subject是单个词
                                    h_end_index = h_start_index
                                    sub = self.decode_entity(text, mapping, h_start_index, h_end_index)
                                    obj = self.decode_entity(text, mapping, t_start_index, t_end_index)
                                    if len(sub) > 0 and len(obj) > 0:
                                        triple_list.append((sub, rel, obj))
                        # object 是单个词
                        if not find_hb_te:
                            t_end_index = t_start_index
                            find_he_te = False
                            for h_end_index in range(h_start_index, seq_lens):
                                # 向下找到了结尾位置,即subject end，object end
                                if rel_triple_matrix[h_end_index][t_end_index] == TAG2ID['HE-TE']:
                                    sub = self.decode_entity(text, mapping, h_start_index, h_end_index)
                                    obj = self.decode_entity(text, mapping, t_start_index, t_end_index)
                                    if len(sub) > 0 and len(obj) > 0:
                                        triple_list.append((sub, rel, obj))
                                        find_he_te = True
                                    # break
                            if not find_he_te:
                                # subject是单个词，且object 是单个词
                                h_end_index = h_start_index
                                sub = self.decode_entity(text, mapping, h_start_index, h_end_index)
                                obj = self.decode_entity(text, mapping, t_start_index, t_end_index)
                                if len(sub) > 0 and len(obj) > 0:
                                    triple_list.append((sub, rel, obj))
            batch_triple_list.append(triple_list)
        return batch_triple_list

    def decode_entity(self, text: str, mapping, start: int, end: int):
        s = mapping[start]
        e = mapping[end]
        s = 0 if not s else s[0]
        e = len(text) - 1 if not e else e[-1]
        entity = text[s: e + 1]
        return entity

    def validation_epoch_end(self, outputs):
        preds, targets, texts = [], [], []
        for pred, target, text in outputs:
            preds.extend(pred)
            targets.extend(target)
            texts.extend(text)

        correct = 0
        predict = 0
        total = 0
        orders = ['subject', 'relation', 'object']

        os.makedirs(os.path.join(self.args.output_path,
                    self.args.model_type), exist_ok=True)
        writer = open(os.path.join(self.args.output_path, self.args.model_type,
                      'val_output_{}.json'.format(self.epoch)), 'w')
        for text, pred, target in zip(*(texts, preds, targets)):
            pred = set([tuple(l) for l in pred])
            target = set([tuple(l) for l in target])
            correct += len(set(pred) & (target))
            predict += len(set(pred))
            total += len(set(target))
            new = [dict(zip(orders, triple)) for triple in pred - target]
            lack = [dict(zip(orders, triple)) for triple in target - pred]
            if len(new) or len(lack):
                result = json.dumps({
                                    'text': text,
                                    'golds': [
                                        dict(zip(orders, triple)) for triple in target
                                    ],
                                    'preds': [
                                        dict(zip(orders, triple)) for triple in pred
                                    ],
                                    'new': new,
                                    'lack': lack
                                    }, ensure_ascii=False)
                writer.write(result + '\n')
        writer.close()

        self.epoch += 1
        real_acc = round(correct/predict, 5) if predict != 0 else 0
        real_recall = round(correct/total, 5)
        real_f1 = round(2*(real_recall*real_acc)/(real_recall +
                        real_acc), 5) if (real_recall+real_acc) != 0 else 0
        self.log("tot", total, prog_bar=True)
        self.log("cor", correct, prog_bar=True)
        self.log("pred", predict, prog_bar=True)
        self.log("recall", real_recall, prog_bar=True)
        self.log("acc", real_acc, prog_bar=True)
        self.log("f1", real_f1, prog_bar=True)

        only_sub_rel_cor = 0
        only_sub_rel_pred = 0
        only_sub_rel_tot = 0
        for pred, target in zip(*(preds, targets)):
            pred = [list(l) for l in pred]
            pred = [(l[0], l[1]) for l in pred if len(l)]
            target = [(l[0], l[1]) for l in target]
            only_sub_rel_cor += len(set(pred).intersection(set(target)))
            only_sub_rel_pred += len(set(pred))
            only_sub_rel_tot += len(set(target))

        real_acc = round(only_sub_rel_cor/only_sub_rel_pred,
                         5) if only_sub_rel_pred != 0 else 0
        real_recall = round(only_sub_rel_cor/only_sub_rel_tot, 5)
        real_f1 = round(2*(real_recall*real_acc)/(real_recall +
                        real_acc), 5) if (real_recall+real_acc) != 0 else 0
        self.log("sr_tot", only_sub_rel_tot, prog_bar=True)
        self.log("sr_cor", only_sub_rel_cor, prog_bar=True)
        self.log("sr_pred", only_sub_rel_pred, prog_bar=True)
        self.log("sr_rec", real_recall, prog_bar=True)
        self.log("sr_acc", real_acc, prog_bar=True)
        self.log("sr_f1", real_f1, prog_bar=True)

    def configure_optimizers(self):
        """[配置优化参数]
        """
        param_optimizer = list(self.named_parameters())
        no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in param_optimizer if not any(
                nd in n for nd in no_decay) and 'bert' in n], 'weight_decay': 0.8, 'lr':2e-5},
            {'params': [p for n, p in param_optimizer if any(
                nd in n for nd in no_decay) and 'bert' in n], 'weight_decay': 0.0, 'lr':2e-5},
            {'params': [p for n, p in param_optimizer if not any(
                nd in n for nd in no_decay) and 'bert' not in n], 'weight_decay': 0.8, 'lr':2e-4},
            {'params': [p for n, p in param_optimizer if any(
                nd in n for nd in no_decay) and 'bert' not in n], 'weight_decay': 0.0, 'lr':2e-4}
        ]

        # optimizer = torch.optim.AdamW(self.parameters(), lr=1e-5)
        optimizer = torch.optim.Adam(self.parameters(), lr=5e-5)
        # StepLR = torch.optim.lr_scheduler.ExponentialLR(optimizer, gamma=0.99)
        milestones = list(range(2, 50, 2))
        scheduler = torch.optim.lr_scheduler.MultiStepLR(
            optimizer, milestones=milestones, gamma=0.85)
        # scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, "min", verbose = True, patience = 6)
        # scheduler = torch.optim.lr_scheduler.StepLR(
        #     optimizer, step_size=self.args.decay_steps, gamma=self.args.decay_rate)
        # scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(optimizer, self.num_step * self.args.rewarm_epoch_num, self.args.T_mult)
        # StepLR = WarmupLR(optimizer,25000)
        optim_dict = {'optimizer': optimizer, 'lr_scheduler': scheduler}
        return optim_dict



class OneRelDataset(Dataset):
    def __init__(self, filename, args, is_training):
        self.tokenizer = BertTokenizerFast.from_pretrained(
            args.pretrain_path, cache_dir="./bertbaseuncased")
        with open(args.relation, 'r') as f:
            relation = json.load(f)
        self.rel2id = relation[1]
        self.rels_set = list(self.rel2id.values())
        self.relation_size = len(self.rel2id)
        self.args = args
        with open(filename, 'r') as f:
            lines = json.load(f)

        if is_training:
            self.datas = self.preprocess_train(lines)
        else:
            self.datas = self.preprocess_val(lines)

    def preprocess_val(self, lines):
        datas = []
        for line in tqdm(lines):
            root_text = line['text']
            tokens = self.tokenizer.tokenize(root_text)
            tokens = [self.tokenizer.cls_token]+ tokens + [self.tokenizer.sep_token]
            if len(tokens) > 512:
                tokens = tokens[: 512]
            text_len = len(tokens)

            token_output = self.tokenizer(root_text,return_offsets_mapping=True)
            token_ids = token_output['input_ids']
            masks = token_output['attention_mask']
            offset_mapping = token_output['offset_mapping']
            if len(token_ids) > text_len:
                token_ids = token_ids[:text_len]
                masks = masks[:text_len]
            token_ids = np.array(token_ids)
            masks = np.array(masks)
            loss_masks = masks
            triple_matrix = np.zeros((self.relation_size, text_len, text_len))
            datas.append([token_ids, masks, loss_masks, text_len,offset_mapping,
                         triple_matrix, self.lower(line['triple_list']), tokens, root_text.lower()])
        return datas

    def preprocess_train(self, lines):
        datas = []
        for line in tqdm(lines):
            root_text = line['text']
            tokens = self.tokenizer.tokenize(root_text)
            tokens = [self.tokenizer.cls_token]+ tokens + [self.tokenizer.sep_token]
            if len(tokens) > 512:
                tokens = tokens[: 512]
            text_len = len(tokens)
            s2ro_map = {}
            for triple in line['triple_list']:
                triple = (self.tokenizer.tokenize(triple[0]), triple[1], self.tokenizer.tokenize(triple[2]))
                sub_head_idx = find_head_idx(tokens, triple[0],0)
                obj_head_idx = find_head_idx(tokens, triple[2],sub_head_idx + len(triple[0]))
                if obj_head_idx == -1:
                    obj_head_idx = find_head_idx(tokens, triple[2],0)
                if sub_head_idx != -1 and obj_head_idx != -1:
                    sub = (sub_head_idx, sub_head_idx + len(triple[0]) - 1)
                    if sub not in s2ro_map:
                        s2ro_map[sub] = []
                    s2ro_map[sub].append(
                        (obj_head_idx, obj_head_idx + len(triple[2]) - 1, self.rel2id[triple[1]]))

            if s2ro_map:
                token_output = self.tokenizer(root_text,return_offsets_mapping=True)
                token_ids = token_output['input_ids']
                masks = token_output['attention_mask']
                offset_mapping = token_output['offset_mapping']
                if len(token_ids) > text_len:
                    token_ids = token_ids[:text_len]
                    masks = masks[:text_len]
                mask_length = len(masks)
                token_ids = np.array(token_ids)
                masks = np.array(masks)
                loss_masks = np.ones((mask_length, mask_length))
                triple_matrix = np.zeros(
                    (self.relation_size, text_len, text_len))
                for s in s2ro_map:
                    sub_head = s[0]
                    sub_tail = s[1]
                    for ro in s2ro_map.get((sub_head, sub_tail), []):
                        obj_head, obj_tail, relation = ro
                        # 赋值顺序不能变，先赋值3，在赋值2，最后赋值1，
                        # 当obj_tail和obj_head一致时，即object是单个字，该位置赋值为HB-TB，
                        # 当sub_tail和sub_head一致时，即subject是单个字，该位置赋值为HB-TE，
                        # 当object和subject都相同时，该位置赋值为HB-TB
                        triple_matrix[relation][sub_tail][obj_tail] = TAG2ID['HE-TE']
                        triple_matrix[relation][sub_head][obj_tail] = TAG2ID['HB-TE']
                        triple_matrix[relation][sub_head][obj_head] = TAG2ID['HB-TB'] 

                datas.append([token_ids, masks, loss_masks, text_len,offset_mapping,
                             triple_matrix, self.lower(line['triple_list']), tokens, root_text.lower()])
        return datas

    def lower(self,triples):
        lower_triples = []
        for line in triples:
            line = [l.lower() for l in line]
            lower_triples.append(line)
        return lower_triples

    def __len__(self):
        return len(self.datas)

    def __getitem__(self, idx):
        return self.datas[idx]


def collate_fn(batch, rel_num):
    batch = list(filter(lambda x: x is not None, batch))
    batch.sort(key=lambda x: x[3], reverse=True)
    token_ids, masks, loss_masks, text_len,offset_mappings, triple_matrix, triples, tokens, texts = zip(
        *batch)
    cur_batch_len = len(batch)
    max_text_len = max(text_len)
    batch_token_ids = torch.LongTensor(cur_batch_len, max_text_len).zero_()
    batch_masks = torch.LongTensor(cur_batch_len, max_text_len).zero_()
    batch_loss_masks = torch.LongTensor(
        cur_batch_len, 1, max_text_len, max_text_len).zero_()
    # if use WebNLG_star, modify tag_size 24 to 171
    batch_triple_matrix = torch.LongTensor(
        cur_batch_len, rel_num, max_text_len, max_text_len).zero_()

    for i in range(cur_batch_len):
        batch_token_ids[i, :text_len[i]].copy_(torch.from_numpy(token_ids[i]))
        batch_masks[i, :text_len[i]].copy_(torch.from_numpy(masks[i]))
        batch_loss_masks[i, 0, :text_len[i], :text_len[i]].copy_(
            torch.from_numpy(loss_masks[i]))
        batch_triple_matrix[i, :, :text_len[i], :text_len[i]].copy_(
            torch.from_numpy(triple_matrix[i]))

    return {'token_ids': batch_token_ids,
            'mask': batch_masks,
            'loss_mask': batch_loss_masks,
            'triple_matrix': batch_triple_matrix,
            'triples': triples,
            'tokens': tokens,
            "texts": texts,
            "offset_map":offset_mappings}