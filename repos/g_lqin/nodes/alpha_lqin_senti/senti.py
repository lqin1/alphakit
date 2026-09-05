"""中期动量 —— 与 g_yliu 的短期反转方向相反, 混合时才有抵消可看。"""
PX = "g_common.field_base_px.adj_close_1500"


def handle(ctx):
    n  = ctx.params["window"]
    px = ctx.win(PX, n + 1)
    return px.loc[0] / px.loc[-n] - 1
