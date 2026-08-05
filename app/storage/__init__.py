"""文件 IO 模块。

职责:本地文件读写,替代 GitHub raw fetch。所有路径解析集中在此。
零耦合:只依赖 config(读路径根)+ 标准库 pathlib。
"""
