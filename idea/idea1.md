执行准备上下文，上下文结构示意：（A,B,C表示其他成员，M表示AGENT）

==============================================
发送类工具直接合并，只保留一个发送结果，以避免出现连续两个"role" == ""user
零状态响应示例：
------------------------------------
收到新消息后：
system:系统提示词
user:新消息
A:XXX 
B:XXX

准备上下文后：
system:系统提示词
user:新消息
A:XXX 
B:XXX

发送请求并响应后：
system:系统提示词
user:新消息
A:XXX 
B:XXX
assistant:tool_call send XXX

执行工具后：
request
system: 系统提示词
user: 新消息
    A:XXX 
    B:XXX
assistant:tool_call 发送 XXX
tool:发送完毕

------------------------------------

收到新消息：
system: 系统提示词
user: 新消息
    A:XXX
    B:XXX
assistant:tool_call 发送 XXX
tool:发送完毕
user: 新消息
    A:XXX 

准备上下文，如果这次就合并，就会出现连续两个user：
system: 系统提示词
user: 历史消息
    A:XXX
    B:XXX
    M:XXX
user: 新消息
    A:XXX 

因此先不合并，只把“新消息”替换为“历史消息”：
system: 系统提示词
user: 历史消息
    A:XXX 
    B:XXX
assistant:tool_call 发送 XXX
tool:发送完毕
user: 新消息
    A:XXX 

发送请求并响应后：
system: 系统提示词
user: 历史消息
    A:XXX 
    B:XXX
assistant:tool_call 发送 XXX
tool:发送完毕
user: 新消息
    A:XXX 
assistant:tool_call send XXX

执行工具后：
system: 系统提示词
user: 历史消息
    A:XXX 
    B:XXX
assistant:tool_call 发送 XXX
tool:发送完毕
user: 新消息
    A:XXX 
assistant:tool_call send XXX
tool:发送完毕

---------------------------------
收到新消息：
system: 系统提示词
user: 历史消息
    A:XXX 
    B:XXX
assistant:tool_call send XXX
tool:发送完毕
user: 新消息
    A:XXX 
assistant:tool_call send XXX
tool:发送完毕
user: 新消息
    C:XXX 

准备上下文后：
system: 系统提示词
user: 历史消息
    A:XXX 
    B:XXX
    M:XXX
    A:XXX
assistant:tool_call send XXX
tool:发送完毕
user: 新消息
    C:XXX 

......


==============================================
非发送类工具保留，并作为分界线

准备上下文后：
system: 系统提示词
user: 历史消息
    A:XXX 
    B:XXX
    M:XXX
    ...
    C:XXX
assistant:tool_call send XXX
tool:发送完毕
user: 新消息
    A:XXX 

发送请求并响应后：
system: 系统提示词
user: 历史消息
    A:XXX 
    B:XXX
    M:XXX
    ...
user: 新消息
    A:XXX 
assistant:tool_call bash command

执行工具后：
system: 系统提示词
user: 历史消息
    A:XXX 
    B:XXX
    M:XXX
    ...
user: 新消息
    A:XXX 
assistant:tool_call bash command, read file
tool: 执行结果

新一轮agent循环，准备上下文后:
system: 系统提示词
user: 历史消息
    A:XXX 
    B:XXX
    M:XXX
    A:XXX 
assistant:tool_call bash command, read file
tool: 执行结果
assistant:tool_call send XXX

执行工具后：
system: 系统提示词
user: 历史消息
    A:XXX 
    B:XXX
    M:XXX
    A:XXX 
assistant:tool_call bash command, read file
tool: 执行结果
assistant:tool_call send XXX
tool：发送完毕