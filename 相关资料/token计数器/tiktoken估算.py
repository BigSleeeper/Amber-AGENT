import tiktoken

text = "<|im_start|> <|im_end|>"

model = "gpt-4o"

# 生成编码器
enc = tiktoken.encoding_for_model(model)

# 用编码器编码文本，获取编码列表
token_list = enc.encode(text, allowed_special="all")

# 编码列表的长度就是token数
token = len(token_list)

print(f"{model} token长度：{token}, 编码列表：")
print(token_list,"\n")

models = ["gpt-5.6-sol","gpt-5.6-luna","gpt-5.5"]
for model in models:
    enc = tiktoken.encoding_for_model(model)
    token_list = enc.encode(text, allowed_special="all")
    token = len(token_list)
    print(f"{model} token长度：{token}, 编码列表：")
    print(token_list,"\n")
