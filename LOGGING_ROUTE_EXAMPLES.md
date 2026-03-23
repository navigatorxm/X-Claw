# Logging Integration Examples for All Routes

This document provides copy-paste examples for integrating structured logging into each route module.

---

## Template: Every Route Should Follow This Pattern

```python
"""Route docstring."""
from __future__ import annotations

# ... existing imports ...
from fastapi import APIRouter, Depends, HTTPException, Request  # ADD Request
from logging import get_logger  # ADD THIS

logger = get_logger(__name__)  # ADD THIS

@router.post("")
async def route_handler(
    body: RequestModel,
    request: Request,  # ADD THIS
    # ... other dependencies ...
) -> dict:
    """Route docstring."""
    # Extract request_id at the start
    request_id = getattr(request.state, "request_id", "unknown")

    # Log at decision points
    logger.info(
        "Action description",
        request_id=request_id,
        agent_id=agent_id,
        action="action_name",
        status="status_value",
    )

    try:
        # Perform action
        result = await some_function()

        # Log success
        logger.info(
            "Success message",
            request_id=request_id,
            agent_id=agent_id,
            action="action_name",
            status="success",
        )
        return result

    except SpecificError as exc:
        # Log specific error
        logger.warn(
            "Error message",
            request_id=request_id,
            agent_id=agent_id,
            action="action_name",
            error=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        # Log unexpected error
        logger.error(
            "Unexpected error",
            request_id=request_id,
            agent_id=agent_id,
            action="action_name",
            error=str(exc),
            exception=exc,
        )
        raise
```

---

## Route: `/approve.py` — Approval Decisions

### Decision Endpoint

```python
from logging import get_logger

logger = get_logger(__name__)

@router.post("")
async def decide(
    body: DecisionRequest,
    request: Request,  # ADD THIS
    store: ApprovalStore = Depends(get_approval_store),
    caller: AgentIdentity = Depends(require_permission(Permission.APPROVE)),
) -> dict:
    """Make approval decision."""
    request_id = getattr(request.state, "request_id", "unknown")

    logger.debug(
        "Processing approval decision",
        request_id=request_id,
        agent_id=caller.agent_id,
        action="approve_decision",
        request_id_param=body.request_id,
        decision=body.decision,
    )

    # Validate request exists
    req = store.get(body.request_id)
    if not req:
        logger.warn(
            "Approval request not found",
            request_id=request_id,
            agent_id=caller.agent_id,
            action="approve_decision",
            approval_request_id=body.request_id,
            error="not_found",
        )
        raise HTTPException(status_code=404, detail=f"Request '{body.request_id}' not found")

    # Validate status is pending
    if req.status != ApprovalStatus.PENDING:
        logger.warn(
            "Request not in pending status",
            request_id=request_id,
            agent_id=caller.agent_id,
            action="approve_decision",
            approval_request_id=body.request_id,
            current_status=req.status.value,
            error="invalid_state",
        )
        raise HTTPException(
            status_code=409,
            detail=f"Request is not pending (current status: {req.status.value}).",
        )

    # Validate decision value
    if body.decision not in ("approve", "reject"):
        logger.warn(
            "Invalid decision value",
            request_id=request_id,
            agent_id=caller.agent_id,
            action="approve_decision",
            approval_request_id=body.request_id,
            decision=body.decision,
            error="invalid_input",
        )
        raise HTTPException(
            status_code=400,
            detail="decision must be 'approve' or 'reject'",
        )

    # Update approval request
    try:
        req.decided_by = caller.agent_id
        req.decision_note = body.note
        req.status = (
            ApprovalStatus.APPROVED if body.decision == "approve"
            else ApprovalStatus.REJECTED
        )
        store.update(req)

        # Log decision
        logger.info(
            f"Approval {body.decision}ed",
            request_id=request_id,
            agent_id=caller.agent_id,
            action="approve_decision",
            approval_request_id=body.request_id,
            decision=body.decision,
            status="decided",
        )

        # Execute immediately if requested
        response = {
            "request_id": body.request_id,
            "decision": body.decision,
            "message": f"Request {body.decision}ed",
        }

        if body.decision == "approve" and body.execute_immediately:
            logger.debug(
                "Executing approved trade",
                request_id=request_id,
                agent_id=caller.agent_id,
                action="execute_approved_trade",
                approval_request_id=body.request_id,
            )
            try:
                # Execute logic here...
                response["execution_status"] = "executed"
                logger.info(
                    "Approved trade executed",
                    request_id=request_id,
                    agent_id=caller.agent_id,
                    action="execute_approved_trade",
                    approval_request_id=body.request_id,
                    status="executed",
                )
            except ExecutionError as exc:
                response["execution_error"] = str(exc)
                logger.warn(
                    "Execution of approved trade failed",
                    request_id=request_id,
                    agent_id=caller.agent_id,
                    action="execute_approved_trade",
                    approval_request_id=body.request_id,
                    error=str(exc),
                )

        return response

    except Exception as exc:
        logger.error(
            "Approval decision processing failed",
            request_id=request_id,
            agent_id=caller.agent_id,
            action="approve_decision",
            approval_request_id=body.request_id,
            error=str(exc),
            exception=exc,
        )
        raise
```

### List Pending Endpoint

```python
@router.get("/pending")
async def list_pending(
    request: Request,  # ADD THIS
    store: ApprovalStore = Depends(get_approval_store),
    caller: AgentIdentity = Depends(require_permission(Permission.APPROVE)),
) -> dict:
    """List pending approval requests."""
    request_id = getattr(request.state, "request_id", "unknown")

    logger.debug(
        "Listing pending approvals",
        request_id=request_id,
        agent_id=caller.agent_id,
        action="list_pending_approvals",
    )

    pending = store.list_pending()

    logger.info(
        "Pending approvals retrieved",
        request_id=request_id,
        agent_id=caller.agent_id,
        action="list_pending_approvals",
        count=len(pending),
        status="success",
    )

    return {
        "count": len(pending),
        "requests": [r.to_dict() for r in pending],
    }
```

---

## Route: `/auth.py` — Agent Registration

### Register Endpoint

```python
from logging import get_logger

logger = get_logger(__name__)

@router.post("/agents")
async def _do_register(
    body: RegisterAgentRequest,
    request: Request,  # ADD THIS
    store: AgentStore = Depends(get_agent_store),
) -> dict:
    """Register a new agent."""
    request_id = getattr(request.state, "request_id", "unknown")

    logger.debug(
        "Agent registration started",
        request_id=request_id,
        agent_id=body.agent_id,
        action="register_agent",
        role=body.role,
    )

    try:
        # Validate role
        try:
            role = Role(body.role)
        except ValueError:
            logger.warn(
                "Invalid role for agent registration",
                request_id=request_id,
                agent_id=body.agent_id,
                action="register_agent",
                role=body.role,
                error="invalid_role",
            )
            raise HTTPException(
                status_code=400,
                detail=f"Unknown role '{body.role}'. Valid: {[r.value for r in Role]}",
            )

        # Validate permissions
        try:
            permissions = [Permission(p) for p in (body.custom_permissions or [])]
        except ValueError as exc:
            logger.warn(
                "Invalid permission in registration",
                request_id=request_id,
                agent_id=body.agent_id,
                action="register_agent",
                error=str(exc),
            )
            raise HTTPException(status_code=400, detail=str(exc))

        # Register agent
        agent = store.register(body.agent_id, role, permissions, body.simulation)

        logger.info(
            "Agent registered successfully",
            request_id=request_id,
            agent_id=body.agent_id,
            action="register_agent",
            role=body.role,
            is_simulation=body.simulation,
            status="created",
            key_prefix=agent.key_prefix,
        )

        return {
            "agent_id": agent.agent_id,
            "role": agent.role.value,
            "permissions": [p.value for p in agent.permissions],
            "key": agent.api_key,
            "key_prefix": agent.key_prefix,
        }

    except ValueError as exc:
        if "already exists" in str(exc):
            logger.warn(
                "Agent already exists",
                request_id=request_id,
                agent_id=body.agent_id,
                action="register_agent",
                error="duplicate_agent",
            )
            raise HTTPException(status_code=409, detail=str(exc))
        raise
    except Exception as exc:
        logger.error(
            "Agent registration failed",
            request_id=request_id,
            agent_id=body.agent_id,
            action="register_agent",
            error=str(exc),
            exception=exc,
        )
        raise
```

---

## Route: `/agents.py` — Wallet Provisioning

### Provision Wallet Endpoint

```python
from logging import get_logger

logger = get_logger(__name__)

@router.post("/register")
async def provision_wallet(
    body: ProvisionWalletRequest,
    request: Request,  # ADD THIS
    store: WalletStore = Depends(get_wallet_store),
    caller: AgentIdentity = Depends(require_permission(Permission.ADMIN)),
) -> dict:
    """Provision a wallet for an agent."""
    request_id = getattr(request.state, "request_id", "unknown")

    logger.debug(
        "Wallet provisioning started",
        request_id=request_id,
        agent_id=body.agent_id,
        action="provision_wallet",
        exchange=body.exchange,
        label=body.label,
    )

    try:
        # Check for duplicates
        existing = store.list(body.agent_id)
        if existing:
            logger.warn(
                "Agent already has wallet(s)",
                request_id=request_id,
                agent_id=body.agent_id,
                action="provision_wallet",
                wallet_count=len(existing),
                error="duplicate_wallet",
            )
            raise HTTPException(
                status_code=409,
                detail=f"Agent '{body.agent_id}' already has {len(existing)} wallet(s).",
            )

        # Create wallet
        wallet = store.create(
            agent_id=body.agent_id,
            label=body.label,
            exchange=body.exchange,
            api_key=body.api_key,
            api_secret=body.api_secret,
        )

        logger.info(
            "Wallet provisioned successfully",
            request_id=request_id,
            agent_id=body.agent_id,
            action="provision_wallet",
            wallet_id=wallet.wallet_id,
            exchange=body.exchange,
            status="created",
        )

        return wallet.to_dict()

    except Exception as exc:
        logger.error(
            "Wallet provisioning failed",
            request_id=request_id,
            agent_id=body.agent_id,
            action="provision_wallet",
            error=str(exc),
            exception=exc,
        )
        raise
```

---

## Route: `/policies.py` — Policy Management

### Create Policy Endpoint

```python
from logging import get_logger

logger = get_logger(__name__)

@router.post("")
async def create_policy(
    body: CreatePolicyRequest,
    request: Request,  # ADD THIS
    store: PolicyStore = Depends(get_policy_store),
    caller: AgentIdentity = Depends(require_permission(Permission.ADMIN)),
) -> dict:
    """Create a new policy."""
    request_id = getattr(request.state, "request_id", "unknown")

    logger.debug(
        "Policy creation started",
        request_id=request_id,
        agent_id=caller.agent_id,
        action="create_policy",
        policy_name=body.name,
        target_agent=body.agent_id,
        rule_count=len(body.rules),
    )

    try:
        policy = store.create(
            agent_id=body.agent_id,
            name=body.name,
            rules=body.rules,
        )

        logger.info(
            "Policy created successfully",
            request_id=request_id,
            agent_id=caller.agent_id,
            action="create_policy",
            policy_id=policy.policy_id,
            policy_name=body.name,
            status="created",
            rule_count=len(body.rules),
        )

        return policy.to_dict()

    except Exception as exc:
        logger.error(
            "Policy creation failed",
            request_id=request_id,
            agent_id=caller.agent_id,
            action="create_policy",
            policy_name=body.name,
            error=str(exc),
            exception=exc,
        )
        raise
```

---

## Route: `/risk.py` — Risk Configuration

### Set Risk Config Endpoint

```python
from logging import get_logger

logger = get_logger(__name__)

@router.post("/config")
async def set_risk_config(
    body: RiskConfigRequest,
    request: Request,  # ADD THIS
    store: RiskConfigStore = Depends(get_risk_store),
    caller: AgentIdentity = Depends(require_permission(Permission.ADMIN)),
) -> dict:
    """Set risk configuration for an agent."""
    request_id = getattr(request.state, "request_id", "unknown")

    logger.debug(
        "Risk config update started",
        request_id=request_id,
        agent_id=caller.agent_id,
        action="set_risk_config",
        target_agent=body.agent_id,
        capital_usd=str(body.total_capital_usd),
    )

    try:
        config = store.upsert(
            agent_id=body.agent_id,
            total_capital_usd=body.total_capital_usd,
            max_daily_drawdown_pct=body.max_daily_drawdown_pct,
            max_trades_per_minute=body.max_trades_per_minute,
            max_trades_per_day=body.max_trades_per_day,
            max_open_exposure_pct=body.max_open_exposure_pct,
            max_open_exposure_approval_pct=body.max_open_exposure_approval_pct,
            max_single_asset_pct=body.max_single_asset_pct,
        )

        logger.info(
            "Risk config updated successfully",
            request_id=request_id,
            agent_id=caller.agent_id,
            action="set_risk_config",
            target_agent=body.agent_id,
            capital_usd=str(body.total_capital_usd),
            status="configured",
        )

        return config.to_dict()

    except Exception as exc:
        logger.error(
            "Risk config update failed",
            request_id=request_id,
            agent_id=caller.agent_id,
            action="set_risk_config",
            target_agent=body.agent_id,
            error=str(exc),
            exception=exc,
        )
        raise
```

---

## Route: `/simulation.py` — Simulation Wallet Management

### Create Simulation Wallet Endpoint

```python
from logging import get_logger

logger = get_logger(__name__)

@router.post("/wallets")
async def create_simulation_wallet(
    body: CreateSimWalletRequest,
    request: Request,  # ADD THIS
    store: WalletStore = Depends(get_wallet_store),
    caller: AgentIdentity = Depends(require_permission(Permission.EXECUTE)),
) -> dict:
    """Create a simulation wallet."""
    request_id = getattr(request.state, "request_id", "unknown")

    logger.debug(
        "Simulation wallet creation started",
        request_id=request_id,
        agent_id=caller.agent_id,
        action="create_sim_wallet",
        target_agent=body.agent_id,
        label=body.label,
    )

    try:
        # Parse balances
        balances = {}
        if body.initial_balances:
            for asset, amount_str in body.initial_balances.items():
                try:
                    balances[asset] = Decimal(amount_str)
                except InvalidOperation as exc:
                    logger.warn(
                        "Invalid balance amount",
                        request_id=request_id,
                        agent_id=caller.agent_id,
                        action="create_sim_wallet",
                        asset=asset,
                        amount=amount_str,
                        error=str(exc),
                    )
                    raise HTTPException(status_code=400, detail=f"Invalid balance amount: {exc}")

        # Create wallet
        wallet = store.create_simulation(
            agent_id=body.agent_id,
            label=body.label,
            initial_balances=balances,
        )

        logger.info(
            "Simulation wallet created successfully",
            request_id=request_id,
            agent_id=caller.agent_id,
            action="create_sim_wallet",
            wallet_id=wallet.wallet_id,
            target_agent=body.agent_id,
            status="created",
        )

        return wallet.to_dict()

    except Exception as exc:
        logger.error(
            "Simulation wallet creation failed",
            request_id=request_id,
            agent_id=caller.agent_id,
            action="create_sim_wallet",
            error=str(exc),
            exception=exc,
        )
        raise
```

---

## Summary

All routes should follow this pattern:

1. ✅ Import `get_logger` and `Request`
2. ✅ Create `logger = get_logger(__name__)`
3. ✅ Add `request: Request` parameter
4. ✅ Extract `request_id` at the start
5. ✅ Log at all decision points (debug, info, warn, error)
6. ✅ Include relevant context fields (agent_id, action, status)
7. ✅ Log exceptions with stack trace

This provides complete traceability across all X-Claw operations.
