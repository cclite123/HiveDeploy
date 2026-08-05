from fastapi import HTTPException


MANAGED_SERVICES = ("astrbot", "napcat", "llonebot")
BOT_SERVICES = ("napcat", "llonebot")


def available_instance_services(user) -> tuple[str, ...]:
    """Return services actually deployed for a user's current instance."""
    instance = getattr(user, "instance", None)
    if not instance:
        return ()
    services = ["astrbot"]
    bot_type = getattr(instance, "bot_type", None) or "napcat"
    if bot_type in BOT_SERVICES:
        services.append(bot_type)
    return tuple(services)


def ensure_instance_service(user, service: str) -> None:
    if service not in MANAGED_SERVICES or service not in available_instance_services(user):
        raise HTTPException(status_code=404, detail="服务未部署")
