import os
from http import HTTPStatus
from typing import Dict, Iterator, List, Optional

import dashscope

from qwen_agent.llm.base import BaseChatModel


class QwenChatAtDS(BaseChatModel):

    def __init__(self, model: str, api_key: str):
        super().__init__()
        self.model = model
        dashscope.api_key = api_key.strip() or os.getenv('DASHSCOPE_API_KEY',
                                                         default='')
        assert dashscope.api_key, 'DASHSCOPE_API_KEY is required.'

    def _chat_stream(
        self,
        messages: List[Dict],
        stop: Optional[List[str]] = None,
    ) -> Iterator[str]:
        stop = stop or []
        response = dashscope.Generation.call(
            self.model,
            messages=messages,  # noqa
            stop_words=[{
                'stop_str': word,
                'mode': 'exclude'
            } for word in stop],
            top_p=0.8,
            result_format='message',
            stream=True,
        )
        last_len = 0
        delay_len = 5
        in_delay = False
        text = ''
        for trunk in response:
            if trunk.status_code == HTTPStatus.OK:
                text = trunk.output.choices[0].message.content
                if (len(text) - last_len) <= delay_len:
                    in_delay = True
                    continue
                else:
                    in_delay = False
                    real_text = text[:-delay_len]
                    now_rsp = real_text[last_len:]
                    yield now_rsp
                    last_len = len(real_text)
            else:
                err = '\nError code: %s. Error message: %s' % (trunk.code,
                                                               trunk.message)
                if trunk.code == 'DataInspectionFailed':
                    err += '\n错误码: 数据检查失败。错误信息: 输入数据可能包含不适当的内容。'
                text = ''
                yield f'{err}'
        if text and (in_delay or (last_len != len(text))):
            yield text[last_len:]

    def _chat_no_stream(
        self,
        messages: List[Dict],
        stop: Optional[List[str]] = None,
    ) -> str:
        stop = stop or []
        response = dashscope.Generation.call(
            self.model,
            messages=messages,  # noqa
            result_format='message',
            stream=False,
            stop_words=[{
                'stop_str': word,
                'mode': 'exclude'
            } for word in stop],
            top_p=0.8,
        )
        if response.status_code == HTTPStatus.OK:
            return response.output.choices[0].message.content
        else:
            err = 'Error code: %s, error message: %s' % (
                response.code,
                response.message,
            )
            return err
