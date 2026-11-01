"""习惯系统：用户用聊天让它自己创建和修改的工作流（dev/习惯/设计稿.md）。

* ``store.py``     —— 工作区里的读写、校验、git 快照、撤回、手改检测；
* ``author.py``    —— HabitAuthor：把用户原话写成 SKILL.md / habit.json（单独一次请求）；
* ``scheduler.py`` —— time 触发的判断；
* ``runner.py``    —— task 类习惯的运行与预算。
"""
