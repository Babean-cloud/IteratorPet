"""LLM 导演层：模型只出"意图"（去哪 / 看哪 / 说什么），轨迹仍交给仿真的物理。

* ``client.py``   —— OpenAI 兼容的 chat/completions 客户端（默认 DeepSeek），只用标准库；
* ``prompt.py``   —— 角色人设 + 输出格式约定；
* ``director.py`` —— 观察 → 后台线程请求 → 主线程把动作落到 ``SimulationState`` 上。

为什么不逐帧控制：一次请求 1–5 s，仿真 40 TPS。模型给"目的地"，
``Wander`` 的贝塞尔 + 机械臂 + 滑车负责走过去，动作照旧自然、物理照旧不出界。
"""
