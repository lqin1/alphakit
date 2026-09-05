"""短期反转, 波动归一。两个变体共用, 差异全在 params。"""
PX   = "g_common.field_base_px.adj_close_1500"
RVOL = "g_yliu.factor_yliu_liq.rvol20"


def handle(ctx):
    n   = ctx.params["window"]
    px  = ctx.win(PX, n + 1)
    raw = -(px.loc[0] / px.loc[-n] - 1)        # 反转: 跌得多的买
    return raw / ctx.f(RVOL)                    # 波动归一, 单输出直接 return
