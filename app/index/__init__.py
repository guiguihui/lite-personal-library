"""索引构建模块。

封装 vendor/build_pageindex.py 的 build() 为不可变 BuildResult,
供 HTTP 路由层(app.http.routes_index)和后台任务(app.index.status)调用。
"""
