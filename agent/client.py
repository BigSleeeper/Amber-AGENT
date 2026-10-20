import time
import openai
from openai import OpenAI

from utils import term
from config import API_KEY, BASE_URL

client = OpenAI(api_key = API_KEY, 
                base_url = BASE_URL,
                max_retries=0
                )

def create_completion(*,    # 不允许按位置传参
                      client=client, 
                      max_retries=5, 
                      retry_delay=1.0, 
                      **kwargs
                      ) -> openai.types.chat.ChatCompletion:
    
    if max_retries < 0 or retry_delay < 0:
        raise ValueError("重试次数和等待时间不能小于0")

    for retry in range(max_retries + 1):
        try:
            # 发起推理请求
            return client.chat.completions.create(**kwargs)

        except (openai.APIConnectionError, openai.InternalServerError) as error:
            if retry == max_retries:
                print(f"\n{term.red}[失败] 已重试 {max_retries} 次。{term.normal}\n", flush=True)
                raise
            reason = getattr(error, "status_code", type(error).__name__)
            print(
                f"\n{term.red}[重试 {retry + 1}/{max_retries}] "
                f"{reason}, {retry_delay}秒后重试······{term.normal}\n",
                flush=True,
            )
            time.sleep(retry_delay)

            