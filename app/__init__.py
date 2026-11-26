"""展陈工房量具出租与返工服务 (Exhibition-workshop gauge rental & rework service).

选取的复核路线：**随件复核 (accompanying recheck)**。
- 租户接单时为每块展板保留一个仍可触及的参照点；
- 校准补入后逐块重算真长，只对受影响的板开单；
- 参照点可触及 => 现场复核单 (recheck)；
- 参照点已被遮盖 => 不能当场放行，必须回工房 (recall)；
- 工房复测读数会改变具体工单。
"""

__all__ = ["store", "engine", "http_api"]
