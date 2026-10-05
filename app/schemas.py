"""Request/response models for the HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class PoolCreate(BaseModel):
    film_id: str = Field(min_length=1, max_length=128, examples=["film-autumn-river"])
    max_sessions: int = Field(
        ge=1, le=1_000_000,
        description="影片级跨场馆共享放映次数上限（正整数）",
        examples=[10],
    )
    note: Optional[str] = Field(default=None, max_length=500)


class PoolOut(BaseModel):
    id: int
    film_id: str
    max_sessions: int
    used_sessions: int
    remaining_sessions: int
    bound_licenses: int = Field(description="当前绑定该池的授权份数")
    note: Optional[str]
    created_at: str


class LicenseCreate(BaseModel):
    film_id: str = Field(min_length=1, max_length=128, examples=["film-autumn-river"])
    venue_id: str = Field(min_length=1, max_length=128, examples=["hall-1"])
    valid_from: datetime = Field(examples=["2026-10-01T00:00:00Z"])
    valid_to: datetime = Field(examples=["2026-10-31T23:59:59Z"])
    max_sessions: int = Field(ge=1, le=1_000_000, examples=[5])
    pool_id: Optional[int] = Field(
        default=None,
        description="可选：绑定影片级共享额度池；绑定后不可修改。池不存在或影片不符返回 422。",
        examples=[1],
    )
    note: Optional[str] = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _check_window(self) -> "LicenseCreate":
        if self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        return self


class PoolBrief(BaseModel):
    id: int
    film_id: str
    max_sessions: int
    used_sessions: int
    remaining_sessions: int


class RevocationOut(BaseModel):
    reason: str
    revoked_at: str


class LicenseOut(BaseModel):
    id: int
    film_id: str
    venue_id: str
    valid_from: str
    valid_to: str
    max_sessions: int
    used_sessions: int
    remaining_sessions: int
    pool_id: Optional[int] = Field(description="所绑共享额度池 id；未绑定为 null")
    pool: Optional[PoolBrief] = Field(
        description="所绑池的实时已用/剩余快照；未绑定为 null"
    )
    revoked: bool
    revocation: Optional[RevocationOut]
    note: Optional[str]
    created_at: str


class RevokeRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500, examples=["版权方临时撤片"])


class RevokeOut(BaseModel):
    id: int
    license_id: int
    reason: str
    revoked_at: str


class SuspensionCreate(BaseModel):
    suspend_from: datetime = Field(
        description="暂停起（含），ISO-8601 且必须带时区偏移，如 2026-10-05T00:00:00+08:00",
        examples=["2026-10-05T00:00:00+08:00"],
    )
    suspend_to: datetime = Field(
        description="暂停止（不含），ISO-8601 且必须带时区偏移，须严格晚于 suspend_from",
        examples=["2026-10-06T00:00:00+08:00"],
    )
    reason: str = Field(min_length=1, max_length=500, examples=["版权方临时通知暂停放映"])

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value

    @model_validator(mode="after")
    def _check_window(self) -> "SuspensionCreate":
        if self.suspend_from.tzinfo is None or self.suspend_to.tzinfo is None:
            raise ValueError("suspend_from and suspend_to must include a timezone offset")
        if self.suspend_to <= self.suspend_from:
            raise ValueError("suspend_to must be later than suspend_from")
        return self


class SuspensionOut(BaseModel):
    id: int = Field(description="暂停编号")
    license_id: int
    suspend_from: str = Field(description="暂停起（含），统一存储为 UTC")
    suspend_to: str = Field(description="暂停止（不含），统一存储为 UTC")
    reason: str
    created_at: str


class VerifyRequest(BaseModel):
    film_id: str = Field(min_length=1, max_length=128)
    venue_id: str = Field(min_length=1, max_length=128)
    start_time: datetime = Field(
        description="场次开始时间，ISO-8601；不带时区时按 UTC 处理",
        examples=["2026-10-05T19:30:00Z"],
    )
    idempotency_key: str = Field(
        min_length=1, max_length=128,
        description="业务幂等键；重复提交返回原结果，内容变化则 409",
        examples=["order-20261005-0001"],
    )


# 一次集中提交允许的场次数上下限（含端点）
BATCH_MIN_ITEMS = 1
BATCH_MAX_ITEMS = 100


class BatchVerifyRequest(BaseModel):
    """发行方集中提交的一批待核验场次。"""

    items: list[VerifyRequest] = Field(
        min_length=BATCH_MIN_ITEMS,
        max_length=BATCH_MAX_ITEMS,
        description=(
            f"按输入顺序核验的场次列表，{BATCH_MIN_ITEMS}～{BATCH_MAX_ITEMS} 项；"
            "任一项格式错误整批拒绝（422），不落任何流水"
        ),
    )


class CancellationOut(BaseModel):
    reason: str
    cancelled_at: str


class VerificationOut(BaseModel):
    id: int
    idempotency_key: str
    film_id: str
    venue_id: str
    start_time: str
    decision: str = Field(description="approved 或 rejected；取消不回写，保持首次决定")
    license_id: Optional[int] = Field(description="命中并消耗额度的授权 id")
    pool_id: Optional[int] = Field(
        description="本次核准同时消耗的共享池 id；授权未绑池或被拒绝时为 null"
    )
    reject_reason: Optional[str] = Field(
        description="拒绝原因：NO_LICENSE / TIME_OUT_OF_WINDOW / LICENSE_REVOKED / "
                    "LICENSE_SUSPENDED / QUOTA_EXHAUSTED / POOL_EXHAUSTED"
    )
    remaining_quota: Optional[int] = Field(description="本次消耗后该授权自身的剩余额度（快照，取消不回写）")
    pool_remaining_quota: Optional[int] = Field(
        description="本次消耗后所绑共享池的剩余额度（快照，取消不回写）；授权未绑池或被拒绝时为 null"
    )
    verified_at: str
    replay: bool = Field(description="true 表示本次为幂等重放，未重新扣减额度")
    cancelled: bool = Field(description="该流水是否已登记场次取消")
    cancellation: Optional[CancellationOut] = Field(
        description="取消详情（原因与取消时间）；未取消为 null"
    )


class BatchVerificationOut(VerificationOut):
    """单条批量核验结果：完整单场核验结果 + 该项在输入中的位置（从 0 开始）。"""

    index: int = Field(description="该项在提交批次中的输入位置（从 0 开始）")


class BatchVerifyResponse(BaseModel):
    count: int = Field(description="本批核验项数（等于 items 长度）")
    results: list[BatchVerificationOut] = Field(
        description="各项核验结果，顺序与输入 items 完全一致"
    )


class CancelRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500, examples=["放映设备故障，场次取消"])

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value


class CancelOut(BaseModel):
    id: int = Field(description="取消记录编号")
    idempotency_key: str = Field(description="原核验幂等键")
    verification_id: int = Field(description="被取消的核验流水 id")
    license_id: int = Field(description="释放一次已用次数的授权 id")
    pool_id: Optional[int] = Field(description="同步释放一次的共享池 id；授权未绑池为 null")
    reason: str
    cancelled_at: str = Field(description="取消登记时间（UTC）")
    replay: bool = Field(
        description="true 表示重复提交同一取消，返回首次记录，未重复释放额度"
    )


# ----------------------------------------------------------------------
# 一次性开场码领取 / 状态查询 / 场馆核销
# ----------------------------------------------------------------------
class OpeningCodeStatusOut(BaseModel):
    """开场码状态；除首次领取外任何途径都不回传原码。"""

    idempotency_key: str = Field(description="原核验幂等键")
    status: Literal["not_issued", "issued", "redeemed", "voided"] = Field(
        description=(
            "not_issued=尚未领取；issued=已领取未核销；"
            "redeemed=已核销（附回执）；voided=场次取消后立即失效"
        )
    )
    issued_at: Optional[str] = Field(description="首次领取时间（UTC）；尚未领取为 null")
    cancelled_at: Optional[str] = Field(
        description="场次取消时间（UTC），status=voided 时非空"
    )
    redemption: Optional["RedemptionOut"] = Field(
        default=None, description="已核销时附首次核销回执，否则为 null"
    )


class OpeningCodeOut(OpeningCodeStatusOut):
    """领取响应：首次领取（replay=false）回传一次性码，重复领取仅回状态。"""

    code: Optional[str] = Field(
        description="一次性开场码；仅首次领取（replay=false）返回，重复领取为 null"
    )
    replay: bool = Field(
        description="true 表示重复领取，只回状态，原码不会再次泄露"
    )


class RedeemRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64, examples=["OC-3f9a..."])
    venue_id: str = Field(min_length=1, max_length=128, examples=["hall-1"])

    @field_validator("code", "venue_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class RedemptionOut(BaseModel):
    """核销回执；成功一次后重复核销始终返回首次回执（replay=true）。"""

    id: int = Field(description="回执编号")
    idempotency_key: str = Field(description="被核销场次的原核验幂等键")
    film_id: str = Field(description="场次影片")
    venue_id: str = Field(description="核销场馆编号（与核准场馆一致）")
    start_time: str = Field(description="计划开场时刻（UTC）")
    license_id: int = Field(description="关联授权 id")
    pool_id: Optional[int] = Field(description="关联共享池 id；授权未绑池为 null")
    redeemed_at: str = Field(description="实际核销时间（UTC）")
    replay: bool = Field(description="true 表示重复核销返回首次回执，不重复记场次")


OpeningCodeStatusOut.model_rebuild()
