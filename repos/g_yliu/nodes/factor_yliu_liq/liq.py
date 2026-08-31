"""流动性与波动三件套。一次窗口读取算出三个产物, 避免重复读盘。"""
import numpy as np

PX  = "g_common.field_base_px.field_base_px-adj_close_1500"
VOL = "g_common.field_base_px.field_base_px-volume_1500"
RET = "g_common.field_base_px.field_base_px-ret_1d_1500"


def handle(ctx):
    w   = ctx.params["window"]
    px  = ctx.win(PX,  w)          # (w, N)
    vol = ctx.win(VOL, w)
    ret = ctx.win(RET, w)

    dollar = px * vol              # (w, N) 逐日成交额
    return ctx.multi_outputs(
        adv20   = dollar.mean(),                   # (N,) 列向聚合 -> 当日截面
        illiq20 = (ret.abs() / dollar).mean() * 1e6,
        rvol20  = ret.std() * np.sqrt(252),
    )
