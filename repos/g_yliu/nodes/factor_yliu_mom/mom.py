PX = "g_common.field_base_px.adj_close_1500"

def handle(ctx):
    n  = ctx.params["window"]
    px = ctx.win(PX, n + 1)             # (n+1, N) 窗口, 行标签 -(n)…0
    return px.loc[0] / px.loc[-n] - 1   # 单输出直接 return 裸值
