from agent import agent_cnt, agent_loop

# 全局上下文
global_context = []

print("LCC-AGENT V0.0.9")
print("Enter a question, press Enter to send.\n")

while True:
    agent_cnt[0] += 1
    query = input("S%d >> "%agent_cnt[0])
    if query == '':
        query = "检查各工具是否正常"
        print(query)

    global_context.append({"role":"user","content":query})
    print("")
    agent_loop(global_context,query)

    response_content = global_context[-1].get("content")
    print("LCC_AGENT:[",response_content,"]\n")
    agent_cnt[1] = 0
