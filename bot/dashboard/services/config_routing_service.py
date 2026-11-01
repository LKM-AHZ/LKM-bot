"""ConfigRoutingService operations for the Dashboard configuration API."""

from __future__ import annotations

from bot.core.core_lifecycle import BotCoreLifecycle


class ConfigRoutingService:
    def __init__(self, core_lifecycle: BotCoreLifecycle) -> None:
        self.ucr = core_lifecycle.umop_config_router

    def list_routes(self) -> dict:
        return {"routing": self.ucr.umop_to_conf_id}

    async def replace_route_mapping(self, routing: dict[str, str]) -> None:
        await self.ucr.update_routing_data(routing)

    async def replace_routes(self, data: object) -> None:
        payload = data if isinstance(data, dict) else {}
        new_routing = payload.get("routing")
        if not isinstance(new_routing, dict):
            raise ValueError("缺少或错误的路由表数据")
        await self.replace_route_mapping(new_routing)

    async def replace_routes_from_dashboard_payload(self, payload: object) -> str:
        if not isinstance(payload, dict) or not payload:
            raise ValueError("缺少配置数据")
        await self.replace_routes(payload)
        return "更新成功"

    async def upsert_route(self, data: object) -> None:
        payload = data if isinstance(data, dict) else {}
        umo = payload.get("umo")
        conf_id = payload.get("conf_id")
        if not umo or not conf_id:
            raise ValueError("缺少 UMO 或配置文件 ID")
        await self.set_route(str(umo), str(conf_id))

    async def set_route(self, umo: str, config_id: str) -> None:
        if config_id == "default":
            await self.delete_route_by_umo(umo)
            return
        await self.ucr.update_route(umo, config_id)

    async def upsert_route_from_dashboard_payload(self, payload: object) -> str:
        if not isinstance(payload, dict) or not payload:
            raise ValueError("缺少配置数据")
        await self.upsert_route(payload)
        return "更新成功"

    async def delete_route(self, data: object) -> None:
        payload = data if isinstance(data, dict) else {}
        umo = payload.get("umo")
        if not umo:
            raise ValueError("缺少 UMO")
        await self.delete_route_by_umo(str(umo))

    async def delete_route_by_umo(self, umo: str) -> None:
        if umo in self.ucr.umop_to_conf_id:
            del self.ucr.umop_to_conf_id[umo]
            await self.ucr.update_routing_data(self.ucr.umop_to_conf_id)

    async def delete_route_from_dashboard_payload(self, payload: object) -> str:
        if not isinstance(payload, dict) or not payload:
            raise ValueError("缺少配置数据")
        await self.delete_route(payload)
        return "删除成功"
