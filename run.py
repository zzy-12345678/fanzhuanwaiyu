# -*- coding: utf-8 -*-
"""run.py —— 一键启动菜单（用 deps 里的便携 Python 跑，不依赖系统装了什么）

双击 启动.bat 就会进到这里。菜单和提示都用 Python 打印，
所以不管控制台是 GBK 还是 UTF-8，中文都不会乱码。
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable                      # 启动.bat 传进来的便携 Python
ENV = dict(os.environ)
ENV["PYTHONNOUSERSITE"] = "1"
ENV["PATH"] = os.path.join(HERE, "deps", "adb") + os.pathsep + ENV.get("PATH", "")

MENU = """
==================================================
            翻转外语  自动化助手
==================================================
   [1] 卡包答题    （一整包约 25 词，约 3 分钟）
   [2] 卡包答题    （自定义词数）
   [0] 退出
==================================================
"""


def run(script, args=()):
    path = os.path.join(HERE, script)
    if not os.path.isfile(path):
        print("找不到 %s" % path)
        return
    print()
    try:
        subprocess.call([PY, path] + list(args), cwd=HERE, env=ENV)
    except KeyboardInterrupt:
        print("\n已中断")
    print("\n------------------- 结束 -------------------")


def ask(prompt, default=""):
    try:
        value = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return None
    return value or default


def main():
    while True:
        print(MENU)
        choice = ask("请输入序号后回车: ")
        if choice is None:
            return 0
        if choice == "0":
            return 0
        if choice == "1":
            run("卡包.py", ["--无视觉"])
        elif choice == "2":
            words = ask("要走多少词（回车默认 25）: ", "25")
            if words is None:
                return 0
            try:
                steps = str(max(1, min(999, int(words))) * 2)
            except ValueError:
                print("词数要填数字")
                continue
            run("卡包.py", ["--无视觉", steps])
        else:
            continue
        try:
            input("\n按回车回到菜单 ...")
        except (EOFError, KeyboardInterrupt):
            return 0


if __name__ == "__main__":
    sys.exit(main())
