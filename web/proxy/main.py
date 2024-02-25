# 监听 HuiXiangDou:Task queue
import json
import os
import pdb
import shutil
import time
import types
from datetime import datetime, timedelta
# implement time lru cache
from functools import lru_cache, partial, wraps

import pytoml
import redis
from BCEmbedding.tools.langchain import BCERerank
from config import feature_store_base_dir, redis_host, redis_port
from feature_store import FeatureStore
from helper import ErrorCode, Queue, TaskCode, parse_json_str
from langchain.embeddings import HuggingFaceEmbeddings
from loguru import logger
from retriever import Retriever
from worker import Worker


def callback_task_state(feature_store_id: str, code: int, _type: str,
                        status: str):
    db = redis.Redis(host=redis_host(),
                     port=redis_port(),
                     charset='utf-8',
                     decode_responses=True)
    resp = Queue(name='TaskResponse',
                 host=redis_host(),
                 port=redis_port(),
                 charset='utf-8',
                 decode_responses=True)
    target = {
        'feature_store_id': feature_store_id,
        'code': code,
        'type': _type,
        'status': status
    }
    resp.put(json.dumps(target))


class CacheRetriever:

    def __init__(self, config_path: str, max_len: int = 4):
        self.cache = dict()
        self.max_len = max_len
        with open(config_path, encoding='utf8') as f:
            config = pytoml.load(f)['feature_store']
            embedding_model_path = config['embedding_model_path']
            reranker_model_path = config['reranker_model_path']

        # load text2vec and rerank model
        self.embeddings = HuggingFaceEmbeddings(
            model_name=embedding_model_path,
            model_kwargs={'device': 'cuda'},
            encode_kwargs={
                'batch_size': 1,
                'normalize_embeddings': True
            })
        self.embeddings.client = self.embeddings.client.half()
        reranker_args = {
            'model': reranker_model_path,
            'top_n': 3,
            'device': 'cuda',
            'use_fp16': True
        }
        self.reranker = BCERerank(**reranker_args)

    def get(self, fs_id: str):
        if fs_id in self.cache:
            self.cache[fs_id]['time'] = time.time()
            return self.cache[fs_id]['retriever'], None

        BASE = feature_store_base_dir()
        workdir = os.path.join(BASE, fs_id, 'workdir')
        configpath = os.path.join(BASE, fs_id, 'config.ini')
        if not os.path.exists(workdir) or not os.path.exist(configpath):
            return None, 'workdir not exist'

        with open(configpath, encoding='utf8') as f:
            reject_throttle = pytoml.load(
                f)['feature_store']['reject_throttle']

        if len(self.cache) >= self.max_len:
            # drop the oldest one
            del_key = None
            min_time = time.time()
            for key, value in enumerate(self.cache):
                cur_time = value['time']
                if cur_time < min_time:
                    min_time = cur_time
                    del_key = key

            if del_key is not None:
                del_value = self.cache[del_key]
                self.cache.pop(del_key)
                del del_value['retriever']

        retriever = Retriever(embeddings=self.embeddings,
                              reranker=self.reranker,
                              work_dir=workdir,
                              reject_throttle=reject_throttle)
        self.cache[fs_id] = {'retriever': retriever, 'time': time.time()}

    def pop(self, fs_id: str):
        if fs_id not in self.cache:
            return
        del_value = self.cache[fs_id]
        self.cache.pop(fs_id)
        # manually free memory
        del del_value


def callback_chat_state(feature_store_id: str, query_id: str, code: int,
                        status: str, text: str, ref: list):
    que = Queue(name='ChatResponse',
                host=redis_host(),
                port=redis_port(),
                charset='utf-8',
                decode_responses=True)

    target = {
        'feature_store_id': feature_store_id,
        'query_id': query_id,
        'response': {
            'code': code,
            'status': status,
            'text': text,
            'references': ref
        }
    }
    que.put(json.dumps(target))


def format_history(history):
    """format [{sender, content}] to [[user1, bot1],[user2,bot2]..] style."""
    ret = []
    last_id = -1

    user = ''
    concat_text = ''
    for item in history:
        if last_id == -1:
            last_id = item.sender
            concat_text = item.content
            continue
        if last_id == item.sender:
            # 和上一个相同， concat
            concat_text += '\n'
            concat_text += item.content
            continue

        # 和上一个不同，把目前所有的 concat_text 加到 user 或 bot 部分
        if last_id == 0:
            # user message
            user = concat_text
        elif last_id == 1:
            # bot reply
            ret.append([user, concat_text])
            user = ''

        # 把当前的 assign 给 last
        last_id = item.sender
        concat_text = item.content

    # 最后一个元素，处理一下
    if last_id == 0:
        # user message
        ret.append([concat_text, ''])
        logger.warning('chat history should not ends with user')
    elif last_id == 1:
        # bot reply
        ret.append([user, concat_text])

    return ret


def chat_with_featue_store(cache: CacheRetriever,
                           payload: types.SimpleNamespace):
    # "payload": {
    #     "feature_store_id": "STRING",
    #     "query_id": "STRING",
    #     "content": "STRING",
    #     "images": ["STRING"],
    #     "history": [{
    #         "sender": Integer,
    #         "content": "STRING"
    #     }]
    # }

    fs_id = payload.feature_store_id
    query_id = payload.query_id

    chat_state = partial(callback_chat_state,
                         feature_store_id=fs_id,
                         query_id=query_id)
    retriever, error = cache.get(fs_id=fs_id)

    if error is not None:
        chat_state(code=ErrorCode.INTERNAL_ERROR.value,
                   status=ErrorCode.INTERNAL_ERROR.describe(),
                   text='',
                   ref=[])
        return

    BASE = feature_store_base_dir()
    workdir = os.path.join(BASE, fs_id, 'workdir')
    configpath = os.path.join(BASE, fs_id, 'config.ini')
    worker = Worker(work_dir=workdir, config_path=configpath)

    # TODO parse images

    history = format_history(payload.history)
    error, response, references = worker.generate(payload.content, history)
    if error != ErrorCode.SUCCESS:
        chat_state(code=ErrorCode.INTERNAL_ERROR.value,
                   status=ErrorCode.INTERNAL_ERROR.describe(),
                   text='',
                   ref=[])
        return
    chat_state(code=ErrorCode.SUCCESS.value,
               status=ErrorCode.SUCCESS.describe(),
               text=response,
               ref=references)


def build_feature_store(cache: CacheRetriever, payload: types.SimpleNamespace):
    # "payload": {
    #     "name": "STRING",
    #     "feature_store_id": "STRING",
    #     "file_abs_base": "STRING",
    #     "path_list": ["STRING"]
    # }
    abs_base = payload.file_abs_base
    files = payload.file_list
    fs_id = payload.feature_store_id
    path_list = []
    for filename in files:
        path_list.append(os.path.join(abs_base, filename))

    BASE = feature_store_base_dir()
    # build dir and config.ini if not exist
    workdir = os.path.join(BASE, fs_id, 'workdir')
    if not os.path.exists(workdir):
        os.makedirs(workdir)

    repodir = os.path.join(BASE, fs_id, 'repodir')
    if not os.path.exists(repodir):
        os.makedirs(repodir)

    configpath = os.path.join(BASE, fs_id, 'config.ini')
    if not os.path.exists(configpath):
        template_file = 'config-template.ini'
        if not os.path.exists(template_file):
            raise Exception(f'{template_file} not exist')
        shutil.copy(template_file, configpath)

    with open(os.path.join(BASE, fs_id, 'desc'), 'w', encoding='utf8') as f:
        f.write(payload.name)

    fs = FeatureStore(config_path=configpath,
                      embeddings=cache.embeddings,
                      reranker=cache.reranker)
    task_state = partial(callback_task_state,
                         feature_store_id=fs_id,
                         _type=TaskCode.FS_ADD_DOC.value)

    try:
        success_cnt, fail_cnt, skip_cnt = fs.initialize(filepaths=path_list,
                                                        work_dir=workdir)
        if success_cnt == len(path_list):
            # success
            task_state(code=ErrorCode.SUCCESS.value,
                       status=ErrorCode.SUCCESS.describe())
        elif success_cnt == 0:
            task_state(code=ErrorCode.FAILED.value, status='无文件被处理')
        else:
            status = f'完成{success_cnt}个文件，跳过{skip_cnt}个，{fail_cnt}个处理异常。请确认文件格式。'
            task_state(code=ErrorCode.SUCCESS.value, status=status)

    except Exception as e:
        logger.error(str(e))
        task_state(code=ErrorCode.FAILED.value, status=str(e))


def update_sample(cache: CacheRetriever, payload: types.SimpleNamespace):
    # "payload": {
    #     "name": "STRING",
    #     "feature_store_id": "STRING",
    #     "positve_path": "STRING",
    #     "negative_path": "STRING",
    # }

    positive = payload.positive
    negative = payload.negative
    fs_id = payload.feature_store_id

    # check
    task_state = partial(callback_task_state,
                         feature_store_id=fs_id,
                         _type=TaskCode.FS_UPDATE_SAMPLE.value)

    if len(positive) < 1 or len(negative) < 1:
        task_state(code=ErrorCode.BAD_PARAMETER.value,
                   status='正例为空。请根据真实用户问题，填写正例；同时填写几句场景无关闲聊作负例')
        return

    BASE = feature_store_base_dir()
    fs_id = payload.feature_store_id
    workdir = os.path.join(BASE, fs_id, 'workdir')
    repodir = os.path.join(BASE, fs_id, 'repodir')
    configpath = os.path.join(BASE, fs_id, 'config.ini')

    if not os.path.exists(workdir) or not os.path.exists(
            repodir) or not os.path.exists(configpath):
        task_state(code=ErrorCode.INTERNAL_ERROR.value,
                   status='知识库未建立或中途异常，已自动反馈研发。请重新建立知识库。')
        return

    try:
        fs = FeatureStore(config_path=configpath,
                          embeddings=cache.embeddings,
                          reranker=cache.reranker)
        fs.update_throttle(config_path=configpath,
                           work_dir=workdir,
                           good_questions=positive,
                           bad_questions=negative)
        del fs
        task_state(code=ErrorCode.SUCCESS.value,
                   status=ErrorCode.SUCCESS.describe())

    except Exception as e:
        logger.error(str(e))
        task_state(code=ErrorCode.FAILED.value, status=str(e))


def process():
    que = Queue(name='Task',
                host=redis_host(),
                port=redis_port(),
                charset='utf-8',
                decode_responses=True)
    fs_cache = CacheRetriever('config-template.ini')

    while True:
        # try:
        msg, error = parse_json_str(que.get())
        if error is not None:
            raise error
        if msg.type == TaskCode.FS_ADD_DOC.value:
            callback_task_state(feature_store_id=msg.payload.feature_store_id,
                                code=ErrorCode.WORK_IN_PROGRESS.value,
                                _type=msg.type,
                                status=ErrorCode.WORK_IN_PROGRESS.describe())
            fs_cache.pop(msg.payload.feature_store_id)
            build_feature_store(fs_cache, msg.payload)
        elif msg.type == TaskCode.FS_UPDATE_SAMPLE.value:
            callback_task_state(feature_store_id=msg.payload.feature_store_id,
                                code=ErrorCode.WORK_IN_PROGRESS.value,
                                _type=msg.type,
                                status=ErrorCode.WORK_IN_PROGRESS.describe())
            fs_cache.pop(msg.payload.feature_store_id)
            update_sample(fs_cache, msg.payload)
        elif msg.type == TaskCode.CHAT.value:
            chat_with_featue_store(fs_cache, msg.payload)
        else:
            logger.warning(f'unknown type {msg.type}')

        # except Exception as e:
        #     logger.error(str(e))


if __name__ == '__main__':
    process()
