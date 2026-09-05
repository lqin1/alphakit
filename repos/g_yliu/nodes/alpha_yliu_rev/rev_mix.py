"""combo = deps 含 alpha 的普通节点, 不是特殊 kind。

三个上游各自 Σ|w|=1, 混合权重和也是 1.0 —— 但组合后 Σ|w| 会因方向相反处互相抵消
而显著小于 1, 故 ops 必须以 scale 收尾, 否则账本投不满而 Sharpe 看着正常。
"""
A = "g_yliu.alpha_yliu_rev.alpha_yliu_rev_w005-weight"
B = "g_yliu.alpha_yliu_rev.alpha_yliu_rev_w020-weight"
C = "g_lqin.alpha_lqin_senti.weight"


def handle(ctx):
    return 0.4 * ctx.f(A) + 0.3 * ctx.f(B) + 0.3 * ctx.f(C)
