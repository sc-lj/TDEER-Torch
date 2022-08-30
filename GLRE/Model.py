# -*- encoding: utf-8 -*-
'''
@File    :   GLRE_Model.py
@Time    :   2022/08/26 15:43:52
@Author  :   lujun
@Version :   1.0
@License :   (C)Copyright 2021-2022, Liugroup-NLPR-CASIA
@Desc    :   文档级关系抽取算法
'''


import torch
import torch.nn as nn 
from Attention import *
from GLRE.utils import *
import pytorch_lightning as pl
import torch.nn.functional as F
from torch.autograd import Variable
from torch.nn.utils.rnn import pad_sequence
from transformers.models.bert.modeling_bert import BertModel
from transformers.models.bert.tokenization_bert_fast import BertTokenizerFast


class RGCN_Layer(nn.Module):
    """ A Relation GCN module operated on documents graphs. """
    def __init__(self, args, in_dim, mem_dim, num_layers, relation_cnt=5):
        """
        Args:
            args (_type_): _description_
            in_dim (_type_): GCN layer 输入的维度
            mem_dim (_type_): GCN layer 中间层以及输出的维度
            num_layers (_type_): GCN layer 的层数
            relation_cnt (int, optional): _description_. Defaults to 5.
        """
        super().__init__()
        self.layers = num_layers
        self.device = torch.device("cuda" if args.gpu != -1 else "cpu")
        self.mem_dim = mem_dim
        self.relation_cnt = relation_cnt
        self.in_dim = in_dim

        self.in_drop = nn.Dropout(args.gcn_in_drop)
        self.gcn_drop = nn.Dropout(args.gcn_out_drop)

        # gcn layer
        self.W_0 = nn.ModuleList()
        self.W_r = nn.ModuleList()
        # for i in range(self.relation_cnt):
        for i in range(relation_cnt):
            self.W_r.append(nn.ModuleList())

        for layer in range(self.layers):
            input_dim = self.in_dim if layer == 0 else self.mem_dim
            self.W_0.append(nn.Linear(input_dim, self.mem_dim).to(self.device))
            for W in self.W_r:
                W.append(nn.Linear(input_dim, self.mem_dim).to(self.device))

    def forward(self, nodes, adj, section):
        """
        Args:
            nodes (_type_): batch_size * node_size * node_emb, 节点矩阵
            adj (_type_):  batch_size * 5 * node_size * node_size, 邻接矩阵
            section (_type_): (Tensor <B, 3>) #entities/#mentions/#sentences per batch
        Returns:
            _type_: _description_
        """
        gcn_inputs = self.in_drop(nodes)

        maskss = []
        denomss = []
        for batch in range(adj.shape[0]):
            masks = []
            denoms = []
            for i in range(self.relation_cnt):
                denom = torch.sparse.sum(adj[batch, i], dim=1).to_dense()
                t_g = denom + torch.sparse.sum(adj[batch, i], dim=0).to_dense()
                mask = t_g.eq(0)
                denoms.append(denom.unsqueeze(1))
                masks.append(mask)
            denoms = torch.sum(torch.stack(denoms), 0)
            denoms = denoms + 1
            masks = sum(masks)
            maskss.append(masks)
            denomss.append(denoms)
        denomss = torch.stack(denomss) # 40 * 61 * 1

        # sparse rgcn layer
        for l in range(self.layers):
            gAxWs = []
            for j in range(self.relation_cnt):
                gAxW = []

                bxW = self.W_r[j][l](gcn_inputs)
                for batch in range(adj.shape[0]):

                    xW = bxW[batch]  # 255 * 25
                    AxW = torch.sparse.mm(adj[batch][j], xW)  # 255, 25
                    # AxW = AxW/ denomss[batch][j]  # 255, 25
                    gAxW.append(AxW)
                gAxW = torch.stack(gAxW)
                gAxWs.append(gAxW)
            gAxWs = torch.stack(gAxWs, dim=1)
            # print("denomss", denomss.shape)
            # print((torch.sum(gAxWs, 1) + self.W_0[l](gcn_inputs)).shape)
            gAxWs = F.relu((torch.sum(gAxWs, 1) + self.W_0[l](gcn_inputs)) / denomss)  # self loop
            gcn_inputs = self.gcn_drop(gAxWs) if l < self.layers - 1 else gAxWs

        return gcn_inputs, maskss


class Local_rep_layer(nn.Module):
    def __init__(self, args):
        super(Local_rep_layer, self).__init__()
        self.query = args.query
        input_dim = args.rgcn_hidden_dim
        self.device = torch.device("cuda" if args.gpu != -1 else "cpu")

        self.multiheadattention = MultiHeadAttention(input_dim, num_heads=args.att_head_num, dropout=args.att_dropout)
        self.multiheadattention1 = MultiHeadAttention(input_dim, num_heads=args.att_head_num,
                                                     dropout=args.att_dropout)


    def forward(self, info, section, nodes, global_nodes):
        """
            :param info: mention_size * 5  <entity_id, entity_type, start_wid, end_wid, sentence_id, origin_sen_id, node_type>
            :param section batch_size * 3 <entity_size, mention_size, sen_size>
            :param nodes <batch_size * node_size>
        """
        entities, mentions, sentences = nodes  # entity_size * dim
        entities = split_n_pad(entities, section[:, 0])  # batch_size * entity_size * -1
        if self.query == 'global':
            entities = global_nodes

        entity_size = section[:, 0].max()
        mentions = split_n_pad(mentions, section[:, 1])

        mention_sen_rep = F.embedding(info[:, 4], sentences)  # mention_size * sen_dim
        mention_sen_rep = split_n_pad(mention_sen_rep, section[:, 1])

        eid_ranges = torch.arange(0, max(info[:, 0]) + 1).to(self.device)
        eid_ranges = split_n_pad(eid_ranges, section[:, 0], pad=-2)  # batch_size * men_size


        r_idx, c_idx = torch.meshgrid(torch.arange(entity_size).to(self.device),
                                          torch.arange(entity_size).to(self.device))
        query_1 = entities[:, r_idx]  # 2 * 30 * 30 * 128
        query_2 = entities[:, c_idx]

        info = split_n_pad(info, section[:, 1], pad=-1)
        m_ids, e_ids = torch.broadcast_tensors(info[:, :, 0].unsqueeze(1), eid_ranges.unsqueeze(-1))
        index_m = torch.ne(m_ids, e_ids).to(self.device)  # batch_size * entity_size * mention_size
        index_m_h = index_m.unsqueeze(2).repeat(1, 1, entity_size, 1).reshape(index_m.shape[0], entity_size*entity_size, -1).to(self.device)
        index_m_t = index_m.unsqueeze(1).repeat(1, entity_size, 1, 1).reshape(index_m.shape[0], entity_size*entity_size, -1).to(self.device)

        entitys_pair_rep_h, h_score = self.multiheadattention(mention_sen_rep, mentions, query_2, index_m_h)
        entitys_pair_rep_t, t_score = self.multiheadattention1(mention_sen_rep, mentions, query_1, index_m_t)
        return entitys_pair_rep_h, entitys_pair_rep_t


class GLREModule(nn.Module):
    def __init__(self,args,) -> None:
        super().__init__()
        self.bert = BertModel.from_pretrained(args.pretrain_path)
        hidden_size = self.bert.config.hidden_size
        self.pretrain_l_m_linear_re = nn.Linear(hidden_size, args.lstm_dim)
        # 是否对实体类型进行embedding
        if args.types:
            self.type_embed = EmbedLayer(num_embeddings=3,
                                         embedding_dim=args.type_dim,
                                         dropout=0.0)

        # global node rep
        rgcn_input_dim = args.lstm_dim
        if args.types:
            rgcn_input_dim += args.type_dim

        self.rgcn_layer = RGCN_Layer(args, rgcn_input_dim, args.rgcn_hidden_dim, args.rgcn_num_layers, relation_cnt=5)
        self.rgcn_linear_re = nn.Linear(args.rgcn_hidden_dim*2, args.rgcn_hidden_dim)
        self.encoder = EncoderLSTM(input_size=hidden_size,
                                   num_units=args.lstm_dim,
                                   nlayers=args.bilstm_layers,
                                   bidir=True,
                                   dropout=args.drop_i)
        if args.finaldist:
            self.dist_embed_dir = EmbedLayer(num_embeddings=20, embedding_dim=args.dist_dim,
                                             dropout=0.0,
                                             ignore=10,
                                             freeze=False,
                                             pretrained=None,
                                             mapping=None)
                                             
        if args.rgcn_num_layers == 0:
            input_dim = rgcn_input_dim * 2
        else:
            input_dim = args.rgcn_hidden_dim * 2

        if args.local_rep:
            self.local_rep_layer = Local_rep_layer(args)
            if not args.global_rep:
                input_dim = args.lstm_dim * 2
            else:
                input_dim += args.lstm_dim* 2

        if args.finaldist:
            input_dim += args.dist_dim * 2


        if args.context_att:
            self.self_att = SelfAttention(input_dim, 1.0)
            input_dim = input_dim * 2

        self.mlp_layer = args.mlp_layers
        if self.mlp_layer>-1:
            hidden_dim = args.mlp_dim
            layers = [nn.Linear(input_dim, hidden_dim), nn.ReLU()]
            for _ in range(args.mlp_layers - 1):
                layers += [nn.Linear(hidden_dim, hidden_dim), nn.ReLU()]
            self.out_mlp = nn.Sequential(*layers)
            input_dim = hidden_dim

        self.classifier = Classifier(in_size=input_dim,
                                     out_size=args.rel_size,
                                     dropout=args.drop_o)

        self.rel_size =args.rel_size
        self.finaldist = args.finaldist
        self.context_att = args.context_att
        self.pretrain_l_m = args.pretrain_l_m
        self.local_rep = args.local_rep
        self.query = args.query
        assert self.query == 'init' or self.query == 'global'
        self.global_rep = args.global_rep
        self.lstm_encoder = args.lstm_encoder
        self.more_lstm = args.more_lstm

        self.dataset = args.dataset

    def encoding_layer(self, word_vec, seq_lens):
        """
        Encoder Layer -> Encode sequences using BiLSTM.
        @:param word_sec [list]
        @:param seq_lens [list]
        """
        ys, _ = self.encoder(torch.split(word_vec, seq_lens.tolist(), dim=0), seq_lens)  # 20, 460, 128
        return ys

    def graph_layer(self, nodes, info, section):
        """
        Graph Layer -> Construct a document-level graph
        The graph edges hold representations for the connections between the nodes.
        Args:
            nodes:
            info:        (Tensor, 5 columns) entity_id, entity_type, start_wid, end_wid, sentence_id
            section:     (Tensor <B, 3>) #entities/#mentions/#sentences per batch
            positions:   distances between nodes (only M-M and S-S)

        Returns: (Tensor) graph, (Tensor) tensor_mapping, (Tensors) indices, (Tensor) node information
        """

        # all nodes in order: entities - mentions - sentences
        nodes = torch.cat(nodes, dim=0)  # e + m + s (all)
        nodes_info = self.node_info(section, info)                 # info/node: node type | semantic type | sentence ID


        nodes = torch.cat((nodes, self.type_embed(nodes_info[:, 0])), dim=1)

        # re-order nodes per document (batch)
        nodes = self.rearrange_nodes(nodes, section)
        nodes = split_n_pad(nodes, section.sum(dim=1))  # torch.Size([4, 76, 210]) batch_size * node_size * node_emb

        nodes_info = self.rearrange_nodes(nodes_info, section)
        nodes_info = split_n_pad(nodes_info, section.sum(dim=1), pad=-1)  # torch.Size([4, 76, 3]) batch_size * node_size * node_type_size

        return nodes, nodes_info

    def node_layer(self, encoded_seq, info, word_sec):
        # SENTENCE NODES
        sentences = torch.mean(encoded_seq, dim=1)  # sentence nodes (avg of sentence words)

        # MENTION & ENTITY NODES
        encoded_seq_token = rm_pad(encoded_seq, word_sec)
        mentions = self.merge_tokens(info, encoded_seq_token)
        entities = self.merge_mentions(info, mentions)  # entity nodes
        return (entities, mentions, sentences)

    @staticmethod
    def merge_tokens(info, enc_seq, type="mean"):
        """
        Merge tokens into mentions;
        Find which tokens belong to a mention (based on start-end ids) and average them
        @:param enc_seq all_word_len * dim  4469*192
        """
        mentions = []
        for i in range(info.shape[0]):
            if type == "max":
                mention = torch.max(enc_seq[info[i, 2]: info[i, 3], :], dim=-2)[0]
            else:  # mean
                mention = torch.mean(enc_seq[info[i, 2]: info[i, 3], :], dim=-2)
            mentions.append(mention)
        mentions = torch.stack(mentions)
        return mentions

    @staticmethod
    def merge_mentions(info, mentions, type="mean"):
        """
        Merge mentions into entities;
        Find which rows (mentions) have the same entity id and average them
        """
        m_ids, e_ids = torch.broadcast_tensors(info[:, 0].unsqueeze(0),
                                               torch.arange(0, max(info[:, 0]) + 1).unsqueeze(-1).to(info.device))
        index_f = torch.ne(m_ids, e_ids).bool().to(info.device)
        entities = []
        for i in range(index_f.shape[0]):
            entity = pool(mentions, index_f[i, :].unsqueeze(-1), type=type)
            entities.append(entity)
        entities = torch.stack(entities)
        return entities


    def node_info(self, section, info):
        """
        info:        (Tensor, 5 columns) entity_id, entity_type, start_wid, end_wid, sentence_id
        Col 0: node type | Col 1: semantic type | Col 2: sentence id
        """
        typ = torch.repeat_interleave(torch.arange(3).to(self.device), section.sum(dim=0))  # node types (0,1,2)
        rows_ = torch.bincount(info[:, 0]).cumsum(dim=0)
        rows_ = torch.cat([torch.tensor([0]).to(self.device), rows_[:-1]]).to(self.device)  #

        stypes = torch.neg(torch.ones(section[:, 2].sum())).to(self.device).long()  # semantic type sentences = -1
        all_types = torch.cat((info[:, 1][rows_], info[:, 1], stypes), dim=0)
        sents_ = torch.arange(section.sum(dim=0)[2]).to(self.device)
        sent_id = torch.cat((info[:, 4][rows_], info[:, 4], sents_), dim=0)  # sent_id
        return torch.cat((typ.unsqueeze(-1), all_types.unsqueeze(-1), sent_id.unsqueeze(-1)), dim=1)

    @staticmethod
    def rearrange_nodes(nodes, section):
        """
        Re-arrange nodes so that they are in 'Entity - Mention - Sentence' order for each document (batch)
        """
        tmp1 = section.t().contiguous().view(-1).long().to(nodes.device)
        tmp3 = torch.arange(section.numel()).view(section.size(1),
                                                  section.size(0)).t().contiguous().view(-1).long().to(nodes.device)
        tmp2 = torch.arange(section.sum()).to(nodes.device).split(tmp1.tolist())
        tmp2 = pad_sequence(tmp2, batch_first=True, padding_value=-1)[tmp3].view(-1)
        tmp2 = tmp2[(tmp2 != -1).nonzero().squeeze()]  # remove -1 (padded)

        nodes = torch.index_select(nodes, 0, tmp2)
        return nodes

    @staticmethod
    def select_pairs(nodes_info, idx, dataset='docred'):
        """
        Select (entity node) pairs for classification based on input parameter restrictions (i.e. their entity type).
        """
        sel = torch.zeros(nodes_info.size(0), nodes_info.size(1), nodes_info.size(1)).to(nodes_info.device)
        a_ = nodes_info[..., 0][:, idx[0]]
        b_ = nodes_info[..., 0][:, idx[1]]
        # 针对不同数据
        if dataset == 'cdr':
            c_ = nodes_info[..., 1][:, idx[0]]
            d_ = nodes_info[..., 1][:, idx[1]]
            condition1 = torch.eq(a_, 0) & torch.eq(b_, 0) & torch.ne(idx[0], idx[1])  # needs to be an entity node (id=0)
            condition2 = torch.eq(c_, 1) & torch.eq(d_, 2)  # h=medicine, t=disease
            sel = torch.where(condition1 & condition2, torch.ones_like(sel), sel)
        else:
            condition1 = torch.eq(a_, 0) & torch.eq(b_, 0) & torch.ne(idx[0], idx[1])
            sel = torch.where(condition1, torch.ones_like(sel), sel)
        return sel.nonzero().unbind(dim=1), sel.nonzero()[:, 0]

    def forward(self, input_ids,attention_mask,token_starts,section,word_sec,entities,rgcn_adjacency,distances_dir,multi_relations):
        """_summary_

        Args:
            input_ids (_type_): 输入的input ids
            attention_mask (_type_): 输入的attention mask
            token_starts (_type_): 输入的每个token的起始位置在input ids中的位置,毕竟有些token会被分为两个input id
            section (_type_): _description_
            word_sec (_type_): _description_
            entities (_type_): _description_
            rgcn_adjacency (_type_): _description_
            distances_dir (_type_): _description_
            multi_relations (_type_): _description_

        Returns:
            _type_: _description_
        """
        context_output = self.bert(input_ids, attention_mask=attention_mask)[0]

        context_output = [layer[starts.nonzero().squeeze(1)] for layer, starts in
                            zip(context_output, token_starts)]
        context_output_pad = []
        for output, word_len in zip(context_output, section[:, 3]):
            if output.size(0) < word_len:
                padding = Variable(output.data.new(1, 1).zero_())
                output = torch.cat([output, padding.expand(word_len - output.size(0), output.size(1))], dim=0)
            context_output_pad.append(output)

        context_output = torch.cat(context_output_pad, dim=0)

        if self.more_lstm:
            context_output = self.encoding_layer(context_output, section[:, 3])
            context_output = rm_pad(context_output, section[:, 3])
        encoded_seq = self.pretrain_l_m_linear_re(context_output)

        encoded_seq = split_n_pad(encoded_seq, word_sec)

        # Graph
        nodes = self.node_layer(encoded_seq, entities, word_sec)

        init_nodes = nodes
        nodes, nodes_info = self.graph_layer(nodes, entities, section[:, 0:3])
        nodes, _ = self.rgcn_layer(nodes, rgcn_adjacency,section[:, 0:3])
        entity_size = section[:, 0].max()
        r_idx, c_idx = torch.meshgrid(torch.arange(entity_size).to(self.device),
                                      torch.arange(entity_size).to(self.device))
        relation_rep_h = nodes[:, r_idx]
        relation_rep_t = nodes[:, c_idx]
        # relation_rep = self.rgcn_linear_re(relation_rep)  # global node rep

        if self.local_rep:
            entitys_pair_rep_h, entitys_pair_rep_t = self.local_rep_layer(entities, section, init_nodes, nodes)
            if not self.global_rep:
                relation_rep_h = entitys_pair_rep_h
                relation_rep_t = entitys_pair_rep_t
            else:
                relation_rep_h = torch.cat((relation_rep_h, entitys_pair_rep_h), dim=-1)
                relation_rep_t = torch.cat((relation_rep_t, entitys_pair_rep_t), dim=-1)

        if self.finaldist:
            dis_h_2_t = distances_dir+ 10
            dis_t_2_h = -distances_dir + 10
            dist_dir_h_t_vec = self.dist_embed_dir(dis_h_2_t)
            dist_dir_t_h_vec = self.dist_embed_dir(dis_t_2_h)
            relation_rep_h = torch.cat((relation_rep_h, dist_dir_h_t_vec), dim=-1)
            relation_rep_t = torch.cat((relation_rep_t, dist_dir_t_h_vec), dim=-1)
        graph_select = torch.cat((relation_rep_h, relation_rep_t), dim=-1)

        if self.context_att:
            relation_mask = torch.sum(torch.ne(multi_relations, 0), -1).gt(0)
            graph_select = self.self_att(graph_select, graph_select, relation_mask)

        # Classification
        r_idx, c_idx = torch.meshgrid(torch.arange(nodes_info.size(1)).to(self.device),
                                      torch.arange(nodes_info.size(1)).to(self.device))
        select, _ = self.select_pairs(nodes_info, (r_idx, c_idx), self.dataset)
        graph_select = graph_select[select]
        if self.mlp_layer>-1:
            graph_select = self.out_mlp(graph_select)
        graph = self.classifier(graph_select)

        return graph,select

class GLREModuelPytochLighting(pl.LightningModule):
    def __init__(self, args):
        super().__init__()
        self.model = GLREModule(args)
        self.rel_size = args.rel_size
        self.ignore_label = ''
    
    def count_predictions(self, y, t):
        """
        Count number of TP, FP, FN, TN for each relation class
        """
        label_num = torch.as_tensor([self.rel_size]).long().to(self.device)
        ignore_label = torch.as_tensor([self.ignore_label]).long().to(self.device)

        mask_t = torch.eq(t, ignore_label).view(-1)          # where the ground truth needs to be ignored
        mask_p = torch.eq(y, ignore_label).view(-1)          # where the predicted needs to be ignored

        true = torch.where(mask_t, label_num, t.view(-1).long().to(self.device))  # ground truth
        pred = torch.where(mask_p, label_num, y.view(-1).long().to(self.device))  # output of NN

        tp_mask = torch.where(torch.eq(pred, true), true, label_num)
        fp_mask = torch.where(torch.ne(pred, true), pred, label_num)
        fn_mask = torch.where(torch.ne(pred, true), true, label_num)

        tp = torch.bincount(tp_mask, minlength=self.rel_size + 1)[:self.rel_size]
        fp = torch.bincount(fp_mask, minlength=self.rel_size + 1)[:self.rel_size]
        fn = torch.bincount(fn_mask, minlength=self.rel_size + 1)[:self.rel_size]
        tn = torch.sum(mask_t & mask_p)
        return {'tp': tp, 'fp': fp, 'fn': fn, 'tn': tn, 'ttotal': t.shape[0]}

    def estimate_loss(self, pred_pairs, truth, multi_truth):
        """
        Softmax cross entropy loss.
        Args:
            pred_pairs (Tensor): Un-normalized pairs (# pairs, classes)
            multi_truth (Tensor) : (#pairs, rel_size)

        Returns: (Tensor) loss, (Tensors) TP/FP/FN
        """
        multi_mask = torch.sum(torch.ne(multi_truth, 0), -1).gt(0)
        # assert (multi_mask == 1).all()
        pred_pairs = pred_pairs[multi_mask]
        multi_truth = multi_truth[multi_mask]
        truth = truth[multi_mask]
        # label smoothing
        # multi_truth -= self.smoothing * ( multi_truth  - 1. / multi_truth.shape[-1])
        loss = torch.sum(self.loss(pred_pairs, multi_truth)) / (
                torch.sum(multi_mask) * self.rel_size)

        return loss, pred_pairs, multi_truth, multi_mask, truth

    def training_step(self, batches,batch_idx):
        bert_token,bert_mask,bert_starts = batches['bert_token'],batches['bert_mask'],batches['bert_starts']
        section,word_sec,entities = batches['section'],batches['word_sec'],batches['entities']
        rgcn_adj,dist_dir,multi_rel = batches['rgcn_adjacency'],batches['distances_dir'],batches['multi_relations']
        relations = batches['relations']
        graph ,select = self.model(bert_token,bert_mask,bert_starts,section,word_sec,entities,rgcn_adj,dist_dir,multi_rel)
        loss, pred_pairs, multi_truth, mask, truth = self.estimate_loss(graph, relations[select], multi_rel[select])
        
        return loss

    def validation_step(self, batches,batch_idx):
        bert_token,bert_mask,bert_starts = batches['bert_token'],batches['bert_mask'],batches['bert_starts']
        section,word_sec,entities = batches['section'],batches['word_sec'],batches['entities']
        rgcn_adj,dist_dir,multi_rel = batches['rgcn_adjacency'],batches['distances_dir'],batches['multi_relations']
        relations = batches['relations']
        graph ,select = self.model(bert_token,bert_mask,bert_starts,section,word_sec,entities,rgcn_adj,dist_dir,multi_rel)
        loss, pred_pairs, multi_truth, mask, truth = self.estimate_loss(graph, relations[select], multi_rel[select])
        
        pred_pairs = torch.sigmoid(pred_pairs)
        predictions = pred_pairs.data.argmax(dim=1)
        stats = self.count_predictions(predictions, truth)
        
        output = {'tp': [], 'fp': [], 'fn': [], 'tn': [], 'loss': [], 'preds': [], 'true': 0}
        test_info = []
        test_result = []
        output['loss'] += [loss.item()]
        output['tp'] += [stats['tp'].to('cpu').data.numpy()]
        output['fp'] += [stats['fp'].to('cpu').data.numpy()]
        output['fn'] += [stats['fn'].to('cpu').data.numpy()]
        output['tn'] += [stats['tn'].to('cpu').data.numpy()]
        output['preds'] += [predictions.to('cpu').data.numpy()]

        test_infos = batches['info'][select[0].to('cpu').data.numpy(),
                                    select[1].to('cpu').data.numpy(),
                                    select[2].to('cpu').data.numpy()][mask.to('cpu').data.numpy()]
        test_info += [test_infos]

        pred_pairs = pred_pairs.data.cpu().numpy()
        multi_truths = multi_truths.data.cpu().numpy()
        output['true'] += multi_truths.sum() - multi_truths[:, self.loader.label2ignore].sum()

        for pair_id in range(len(pred_pairs)):
            multi_truth = multi_truths[pair_id]
            for r in range(0, self.rel_size):
                if r == self.loader.label2ignore:
                    continue

                test_result.append((int(multi_truth[r]) == 1, float(pred_pairs[pair_id][r]),
                                    test_infos[pair_id]['intrain'],test_infos[pair_id]['cross'], self.loader.index2rel[r], r,
                                    len(test_info) - 1, pair_id))


    def configure_optimizers(self):
        """[配置优化参数]
        """
        param_optimizer = list(self.named_parameters())
        no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
                {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay) and 'bert' in n], 'weight_decay': 0.8,'lr':2e-5},
                {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay) and 'bert' in n], 'weight_decay': 0.0,'lr':2e-5},
                {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay) and 'bert' not in n], 'weight_decay': 0.8,'lr':2e-4},
                {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay) and 'bert' not in n], 'weight_decay': 0.0,'lr':2e-4}
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


