from transformers import AutoTokenizer

# Qwen3.8-27B
tok = AutoTokenizer.from_pretrained("Qwen/Qwen3.8-27B", trust_remote_code=True)
tok.save_pretrained("./tokenizer_Qwen38-27B")

# DeepSeek-V4-Flash
tok = AutoTokenizer.from_pretrained("deepseek-ai/DeepSeek-V4-Flash", trust_remote_code=True)
tok.save_pretrained("./tokenizer_DeepSeek-V4-Flash")

# DeepSeek-V4-Pro
tok = AutoTokenizer.from_pretrained("deepseek-ai/DeepSeek-V4-Pro", trust_remote_code=True)
tok.save_pretrained("./tokenizer_DeepSeek-V4-Pro")

# DeepSeek-V4.1-Flash
tok = AutoTokenizer.from_pretrained("deepseek-ai/DeepSeek-V4.1-Flash", trust_remote_code=True)
tok.save_pretrained("./tokenizer_DeepSeek-V41-Flash")
