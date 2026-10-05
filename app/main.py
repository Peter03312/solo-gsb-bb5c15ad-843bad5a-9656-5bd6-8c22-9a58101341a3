"""FastAPI application exposing the screening-license verification API."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .schemas import (
    BatchVerifyRequest,
    BatchVerifyResponse,
    CancelOut,
    CancelRequest,
    LicenseCreate,
    LicenseOut,
    OpeningCodeOut,
    OpeningCodeStatusOut,
    PoolCreate,
    PoolOut,
    RedeemRequest,
    RedemptionOut,
    RevokeOut,
    RevokeRequest,
    SuspensionCreate,
    SuspensionOut,
    VerificationOut,
    VerifyRequest,
)
from .service import ConflictError, NotFoundError, Store, UnprocessableError
from .timeutil import now_iso

DEFAULT_DB_PATH = os.environ.get("FESTIVAL_DB_PATH", "./filmfest.db")


def _seed_demo(store: Store) -> None:
    """Insert demo licenses when SEED_DEMO_DATA is enabled and DB is empty."""
    if store.count_licenses() > 0:
        return
    now = datetime.now(timezone.utc)
    store.create_license(
        film_id="film-autumn-river",
        venue_id="hall-1",
        valid_from=now - timedelta(days=1),
        valid_to=now + timedelta(days=7),
        max_sessions=2,
        note="demo: 早到期授权，核验时优先消耗",
    )
    store.create_license(
        film_id="film-autumn-river",
        venue_id="hall-1",
        valid_from=now - timedelta(days=1),
        valid_to=now + timedelta(days=30),
        max_sessions=5,
        note="demo: 兜底授权",
    )


def create_app(db_path: Optional[str] = None) -> FastAPI:
    db_path = db_path or DEFAULT_DB_PATH
    store = Store(db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.ensure()  # 启动即完成建表初始化
        if os.environ.get("SEED_DEMO_DATA", "").lower() in {"1", "true", "yes"}:
            _seed_demo(store)
        yield

    app = FastAPI(
        title="Film Festival Screening License Service",
        version="1.4.0",
        description="电影节片源放映许可核验：授权录入、限时暂停、幂等核验、额度控制、撤销追溯、已核准场次取消与一次性开场码领取/核销",
        lifespan=lifespan,
    )
    app.state.store = store

    @app.exception_handler(ConflictError)
    async def _conflict_handler(request: Request, exc: ConflictError):
        content = {"detail": str(exc), "code": "CONFLICT", "existing": exc.existing}
        if exc.index is not None:
            content["index"] = exc.index
        return JSONResponse(status_code=409, content=content)

    @app.exception_handler(NotFoundError)
    async def _not_found_handler(request: Request, exc: NotFoundError):
        return JSONResponse(
            status_code=404,
            content={"detail": str(exc), "code": exc.code},
        )

    @app.exception_handler(UnprocessableError)
    async def _unprocessable_handler(request: Request, exc: UnprocessableError):
        return JSONResponse(
            status_code=422,
            content={"detail": str(exc), "code": exc.code},
        )

    # ------------------------------------------------------------------
    # meta
    # ------------------------------------------------------------------
    @app.get("/", tags=["meta"])
    def root():
        return {
            "service": "film-festival-licensing",
            "version": "1.4.0",
            "docs": "/docs",
            "health": "/health",
        }

    @app.get("/health", tags=["meta"])
    def health():
        return {"status": "ok", "time": now_iso()}

    # ------------------------------------------------------------------
    # quota pools (film-level shared cap across venues)
    # ------------------------------------------------------------------
    @app.post("/pools", response_model=PoolOut, status_code=201, tags=["pools"])
    def create_pool(body: PoolCreate):
        """登记影片级共享额度池（影片 + 正整数上限），供多场馆授权绑定。"""
        return store.create_pool(
            film_id=body.film_id,
            max_sessions=body.max_sessions,
            note=body.note,
        )

    @app.get("/pools", response_model=list[PoolOut], tags=["pools"])
    def list_pools(film_id: Optional[str] = Query(default=None)):
        """列出共享额度池及实时已用/剩余次数，可按 film_id 过滤。"""
        return store.list_pools(film_id=film_id)

    @app.get("/pools/{pool_id}", response_model=PoolOut, tags=["pools"])
    def get_pool(pool_id: int):
        return store.get_pool(pool_id)

    # ------------------------------------------------------------------
    # licenses
    # ------------------------------------------------------------------
    @app.post("/licenses", response_model=LicenseOut, status_code=201, tags=["licenses"])
    def create_license(body: LicenseCreate):
        """录入一份放映授权（影片、场馆、有效时段、场次上限）。

        可选 ``pool_id`` 绑定影片级共享额度池：绑定后不可修改；池不存在
        或池的影片与授权影片不一致时返回 422。不传则保持旧语义。
        """
        return store.create_license(
            film_id=body.film_id,
            venue_id=body.venue_id,
            valid_from=body.valid_from,
            valid_to=body.valid_to,
            max_sessions=body.max_sessions,
            note=body.note,
            pool_id=body.pool_id,
        )

    @app.get("/licenses", response_model=list[LicenseOut], tags=["licenses"])
    def list_licenses(
        film_id: Optional[str] = Query(default=None),
        venue_id: Optional[str] = Query(default=None),
        pool_id: Optional[int] = Query(default=None),
    ):
        """列出授权及实时剩余额度、撤销状态、所绑池快照。"""
        return store.list_licenses(
            film_id=film_id, venue_id=venue_id, pool_id=pool_id
        )

    @app.get("/licenses/{license_id}", response_model=LicenseOut, tags=["licenses"])
    def get_license(license_id: int):
        return store.get_license(license_id)

    @app.post(
        "/licenses/{license_id}/revoke",
        response_model=RevokeOut,
        tags=["licenses"],
    )
    def revoke_license(license_id: int, body: RevokeRequest):
        """撤销授权：仅阻止后续核验，已核准场次记录保留可追溯。"""
        return store.revoke_license(license_id, body.reason)

    # ------------------------------------------------------------------
    # time-limited suspensions
    # ------------------------------------------------------------------
    @app.post(
        "/licenses/{license_id}/suspensions",
        response_model=SuspensionOut,
        status_code=201,
        tags=["suspensions"],
    )
    def create_suspension(license_id: int, body: SuspensionCreate):
        """为一份授权登记限时暂停（左闭右开区间 [suspend_from, suspend_to)）。

        - 起止时刻必须带时区偏移，且结束严格晚于开始；授权不存在返回 404；
        - 区间可重叠，核验时按并集生效；记录一经登记不可修改、不可删除；
        - 暂停只影响此后提交、且场次开始时间落入区间的新核验，已存在的
          核准流水及其额度/池占用不受影响。
        """
        return store.create_suspension(
            license_id,
            suspend_from=body.suspend_from,
            suspend_to=body.suspend_to,
            reason=body.reason,
        )

    @app.get(
        "/licenses/{license_id}/suspensions",
        response_model=list[SuspensionOut],
        tags=["suspensions"],
    )
    def list_license_suspensions(license_id: int):
        """列出单份授权的全部暂停记录（按登记次序）；授权不存在返回 404。"""
        store.get_license(license_id)  # 404 when the license is unknown
        return store.list_suspensions(license_id=license_id)

    @app.get("/suspensions", response_model=list[SuspensionOut], tags=["suspensions"])
    def list_suspensions(
        license_id: Optional[int] = Query(default=None),
    ):
        """列出全部暂停记录，可按 license_id 过滤；记录登记后不可改。"""
        return store.list_suspensions(license_id=license_id)

    @app.get(
        "/suspensions/{suspension_id}",
        response_model=SuspensionOut,
        tags=["suspensions"],
    )
    def get_suspension(suspension_id: int):
        return store.get_suspension(suspension_id)

    # ------------------------------------------------------------------
    # verifications
    # ------------------------------------------------------------------
    @app.post("/verifications", response_model=VerificationOut, tags=["verifications"])
    def verify_screening(body: VerifyRequest):
        """核验一个待放映场次（幂等）。

        - 多份授权同时有效时只消耗一份额度，优先最早到期且自身与所绑池
          均有余额的授权；核准时授权与其池各消耗一次；
        - 暂停中的授权（场次开始时间落入某暂停区间，左闭右开、重叠取并集）
          不参与选择；覆盖该时段且未撤销的授权全部暂停时，拒绝原因为
          LICENSE_SUSPENDED；否则跳过暂停授权后沿用原有拒绝原因及次序；
        - 同一幂等键重试返回原结果（replay=true），请求内容变化返回 409；
        - 拒绝时 decision=rejected 并给出 reject_reason。
        """
        return store.verify(
            film_id=body.film_id,
            venue_id=body.venue_id,
            start_time=body.start_time,
            idempotency_key=body.idempotency_key,
        )

    @app.post(
        "/verifications/batch",
        response_model=BatchVerifyResponse,
        tags=["verifications"],
    )
    def verify_screening_batch(body: BatchVerifyRequest):
        """发行方集中提交一批（1～100 个）场次，按输入顺序在一次请求内核验。

        - 每项沿用单场核验的全部规则（授权选择、共享池、限时暂停、撤销、
          幂等）；严格按顺序处理，**前项消耗的额度/池额度影响后项**，业务
          拒绝同样落一条可追溯流水并继续处理后续项；
        - 同批或历史中同幂等键且内容一致：返回原流水（replay=true），
          **不重复扣额**；同键但内容不一致：**整批失败 409**（响应带
          ``index`` 指出冲突项位置及 ``existing`` 原流水），整批回滚；
        - 任一项格式错误（422，请求未进入处理）或处理中异常：**整批不留
          下任何新流水，也不占用任何额度/池额度**；
        - 整批在单个 ``BEGIN IMMEDIATE`` 事务内完成，与并发的单场核验、
          取消完全串行：彼此只能看到整批之前或之后的完整状态，不会穿插
          超额或重复释放；
        - 响应 ``results`` 与输入同序，每项为完整单场核验结果并附
          ``index``（输入位置，从 0 开始）。
        """
        results = store.verify_batch(
            [
                {
                    "film_id": item.film_id,
                    "venue_id": item.venue_id,
                    "start_time": item.start_time,
                    "idempotency_key": item.idempotency_key,
                }
                for item in body.items
            ]
        )
        return {"count": len(results), "results": results}

    @app.get("/verifications", response_model=list[VerificationOut], tags=["verifications"])
    def list_verifications(
        film_id: Optional[str] = Query(default=None),
        venue_id: Optional[str] = Query(default=None),
        decision: Optional[str] = Query(default=None, pattern="^(approved|rejected)$"),
        license_id: Optional[int] = Query(default=None),
        pool_id: Optional[int] = Query(default=None),
        limit: int = Query(default=200, ge=1, le=1000),
    ):
        """核验流水（含已核准场次的可追溯记录）。"""
        return store.list_verifications(
            film_id=film_id,
            venue_id=venue_id,
            decision=decision,
            license_id=license_id,
            pool_id=pool_id,
            limit=limit,
        )

    @app.get(
        "/verifications/{idempotency_key}",
        response_model=VerificationOut,
        tags=["verifications"],
    )
    def get_verification(idempotency_key: str):
        return store.get_verification(idempotency_key)

    @app.post(
        "/verifications/{idempotency_key}/cancel",
        response_model=CancelOut,
        tags=["verifications"],
    )
    def cancel_verification(idempotency_key: str, body: CancelRequest):
        """按原核验幂等键取消一个已核准且尚未开场的场次（幂等）。

        - 仅已核准（approved）且场次开始时间未到的流水可取消；未知键 404，
          已拒绝流水或已开场的场次 422；
        - 取消成功后该授权及所绑共享池的实时已用次数各回退一次，释放的
          额度可被后续核验使用；原核验决定与扣减时余额快照保持原样；
        - 重复提交相同取消返回首次记录（replay=true），不重复释放额度；
          同一键但取消原因不同返回 409。
        """
        return store.cancel_verification(idempotency_key, reason=body.reason)

    # ------------------------------------------------------------------
    # one-time opening codes (operator claim / status) and venue redemption
    # ------------------------------------------------------------------
    @app.post(
        "/verifications/{idempotency_key}/opening-code",
        response_model=OpeningCodeOut,
        tags=["opening-codes"],
    )
    def claim_opening_code(idempotency_key: str):
        """运营方按原核验幂等键为核准场次领取一次性开场码。

        - 仅已核准（approved）、未取消且未到计划开场时刻的场次可领取；
          未知键 404，已拒绝 422 VERIFICATION_NOT_APPROVED，已取消 422
          SESSION_CANCELLED，已开场 422 SESSION_ALREADY_STARTED，均不产生码；
        - 每场仅有一个码：首次领取返回原码（replay=false），重复领取只返回
          状态（replay=true，code=null），原码不会再次泄露；
        - 取消与领取并发时由写锁与唯一约束串行化：取消先到则领取失败，
          领取先到则取消后该码状态立即变为 voided。
        """
        return store.claim_opening_code(idempotency_key)

    @app.get(
        "/verifications/{idempotency_key}/opening-code",
        response_model=OpeningCodeStatusOut,
        tags=["opening-codes"],
    )
    def get_opening_code_status(idempotency_key: str):
        """查询开场码状态（not_issued/issued/redeemed/voided），不返回原码。"""
        return store.get_opening_code_status(idempotency_key)

    @app.post(
        "/redemptions",
        response_model=RedemptionOut,
        tags=["opening-codes"],
    )
    def redeem_opening_code(body: RedeemRequest):
        """场馆凭开场码及场馆编号在计划开场时刻起 30 分钟内核销。

        - 窗口为左闭右开 ``[start_time, start_time+30min)``：过早 422
          REDEMPTION_TOO_EARLY，过期 422 REDEMPTION_WINDOW_CLOSED；场馆不符
          422 VENUE_MISMATCH；场次已取消 422 SESSION_CANCELLED；未知码 404
          OPENING_CODE_NOT_FOUND；一切拒绝都不产生回执；
        - 成功一次后重复核销返回首次回执（replay=true，即使已过窗口），
          不重复记场次；错场馆重试一律 VENUE_MISMATCH（含已核销码）；
        - 核销不重新校验授权：此后授权撤销或暂停不改变已核准场次的核销
          资格，也不改变原核验结果与额度/池占用。
        """
        return store.redeem_opening_code(code=body.code, venue_id=body.venue_id)

    return app


app = create_app()
