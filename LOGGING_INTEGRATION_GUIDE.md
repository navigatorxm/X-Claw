# X-Claw Structured Logging Integration Guide

## Overview

This guide documents the structured logging system improvements for X-Claw. The system provides:

- **Request ID Correlation** - Unique ID (`req_<12-hex>`) generated per API call, propagated across all components
- **Structured JSON Logging** - Machine-readable logs with timestamp, agent_id, action, decision, status
- **Error Categorization** - Stack traces and error types captured systematically
- **Performance Metrics** - Latency tracking for all requests and operations
- **Audit Trail** - Full traceability of all actions via request_id

---

## Architecture

### Components

```
┌─────────────────┐
│   FastAPI App   │
│   (app.py)      │
└────────┬────────┘
         │
         ├─→ RequestLoggingMiddleware  (request_id generation)
         │
         ├─→ Route Handlers           (structured logging)
         │   └─→ get_logger(request_id)
         │
         ├─→ ExecutionEngine          (decision logging)
         │   └─→ policy/risk/approval gates
         │
         └─→ AuditLogger              (immutable trail)
             └─→ request_id in metadata
```

### Request Flow

```
1. HTTP Request arrives
   ↓
2. RequestLoggingMiddleware
   - Generate request_id (req_<12-hex>)
   - Store in request.state.request_id
   - Log incoming request
   ↓
3. Route Handler (e.g., /execute)
   - Get request_id from request.state
   - Create logger instance
   - Log action with request_id
   - Call ExecutionEngine
   ↓
4. ExecutionEngine
   - Policy/Risk/Approval evaluations
   - Pass request_id to audit_logger.log()
   - Audit entries include request_id in metadata
   ↓
5. Response
   - Log response status and latency
   - Include request_id in response body (optional)
```

---

## Setup

### 1. Logging Module (ALREADY CREATED)

**Files:**
- `/xclaw_finance/logging/__init__.py` - Public API
- `/xclaw_finance/logging/structured_logger.py` - Logger implementation
- `/xclaw_finance/logging/middleware.py` - Request tracking middleware

**Features:**
- `StructuredLogger` class for JSON logging
- `setup_logging()` to configure Python logging module
- `RequestLoggingMiddleware` for request/response tracking
- `ErrorCategory` enum for error classification

### 2. App Configuration (ALREADY UPDATED)

**File:** `/xclaw_finance/api/app.py`

**Changes:**
```python
# Add imports
from logging import setup_logging, LogLevel
from logging.middleware import RequestLoggingMiddleware

# Setup logging
setup_logging(level=LogLevel.INFO)

# Add middleware BEFORE CORS
app.add_middleware(RequestLoggingMiddleware)
```

### 3. Route Integration (PARTIALLY COMPLETED)

**File:** `/xclaw_finance/api/routes/execute.py`

**Changes Made:**
- Added logger import: `from logging import get_logger`
- Updated `execute_trade()` to:
  - Accept `request: Request` parameter
  - Extract request_id: `request_id = getattr(request.state, "request_id", "unknown")`
  - Log all decision points with request_id
  - Pass request_id to engine: `request_id=request_id`
- Added logging for:
  - Authorization failures
  - Simulation restrictions
  - Trade execution start/completion
  - Denials and pending approvals
  - Errors with stack traces

---

## Integration Changes Required

### Routes to Update (Follow `/execute.py` pattern)

Each route should:

1. **Import logger:**
   ```python
   from logging import get_logger
   logger = get_logger(__name__)
   ```

2. **Accept Request parameter:**
   ```python
   async def route_handler(
       ...,
       request: Request,
       ...
   ) -> dict:
   ```

3. **Extract request_id:**
   ```python
   request_id = getattr(request.state, "request_id", "unknown")
   ```

4. **Log at key decision points:**
   ```python
   logger.info(
       "Action description",
       request_id=request_id,
       agent_id=agent_id,
       action="action_name",
       status="status_value",
       **additional_fields,
   )
   ```

#### Routes to Update:

**`/xclaw_finance/api/routes/approve.py`**
- Log `decide()` calls (approved/rejected)
- Log approval_request_id and decision
- Log immediate execution results

**`/xclaw_finance/api/routes/auth.py`**
- Log agent registration
- Log key rotation
- Log role changes

**`/xclaw_finance/api/routes/agents.py`**
- Log wallet provisioning
- Log agent creation

**`/xclaw_finance/api/routes/policies.py`**
- Log policy creation/deletion
- Log policy evaluation results

**`/xclaw_finance/api/routes/risk.py`**
- Log risk config updates
- Log risk evaluation results

**`/xclaw_finance/api/routes/simulation.py`**
- Log simulation wallet creation
- Log portfolio resets

**`/xclaw_finance/api/routes/history.py`**
- (Read-only, minimal logging needed)

---

### Engine Integration

#### ExecutionEngine (`/xclaw_finance/execution_engine/engine.py`)

**Changes Made:**
- Added `request_id: Optional[str] = None` parameter to `execute_trade()`

**Changes Needed:**
- Pass request_id to audit_logger.log() calls:
  ```python
  self._audit.log(
      agent_id=agent_id,
      action="...",
      policy_decision=decision.value,
      approval_chain=...,
      execution_result=...,
      metadata={
          **existing_metadata,
          "request_id": request_id,  # ADD THIS
      }
  )
  ```

#### AuditLogger (`/xclaw_finance/audit_logger/logger.py`)

**Changes Needed:**
- No signature changes required
- request_id will be included in metadata dict
- SQLite schema already supports metadata as JSON

---

## Log Format Examples

### Incoming Request
```json
{
  "timestamp": "2026-03-23T15:30:45.123Z",
  "level": "INFO",
  "logger": "xclaw_finance.logging.middleware",
  "message": "Incoming request",
  "request_id": "req_a1b2c3d4e5f6",
  "agent_id": "admin_001",
  "method": "POST",
  "path": "/execute",
  "query_string": null
}
```

### Trade Execution
```json
{
  "timestamp": "2026-03-23T15:30:45.234Z",
  "level": "INFO",
  "logger": "xclaw_finance.api.routes.execute",
  "message": "Trade executed successfully",
  "request_id": "req_a1b2c3d4e5f6",
  "agent_id": "trader_001",
  "action": "execute_trade",
  "status": "executed",
  "order_id": "ord_x1y2z3"
}
```

### Trade Denied
```json
{
  "timestamp": "2026-03-23T15:30:45.345Z",
  "level": "INFO",
  "logger": "xclaw_finance.api.routes.execute",
  "message": "Trade denied",
  "request_id": "req_a1b2c3d4e5f6",
  "agent_id": "trader_001",
  "action": "execute_trade",
  "status": "denied",
  "source": "risk",
  "reason": "Daily limit exceeded"
}
```

### Error with Stack Trace
```json
{
  "timestamp": "2026-03-23T15:30:45.456Z",
  "level": "ERROR",
  "logger": "xclaw_finance.api.routes.execute",
  "message": "Trade execution error",
  "request_id": "req_a1b2c3d4e5f6",
  "agent_id": "trader_001",
  "action": "execute_trade",
  "status": "error",
  "error": "Wallet not found",
  "exception": {
    "type": "ExecutionError",
    "message": "Wallet 'w_999' not found.",
    "traceback": "Traceback (most recent call last):\n  File ..."
  }
}
```

### Response Logged
```json
{
  "timestamp": "2026-03-23T15:30:45.567Z",
  "level": "INFO",
  "logger": "xclaw_finance.logging.middleware",
  "message": "Response sent",
  "request_id": "req_a1b2c3d4e5f6",
  "agent_id": "trader_001",
  "method": "POST",
  "path": "/execute",
  "status": "success",
  "status_code": 200,
  "latency_ms": 124.56
}
```

---

## Querying Logs

### By Request ID (across all components)

```bash
# Show all logs for a specific request
grep "req_a1b2c3d4e5f6" xclaw_finance/logs/*.log | jq .

# Count steps in request flow
grep "req_a1b2c3d4e5f6" xclaw_finance/logs/*.log | jq '.message'
```

### By Agent ID

```bash
grep '"agent_id": "trader_001"' xclaw_finance/logs/*.log | jq '.timestamp, .message, .status'
```

### By Action Type

```bash
grep '"action": "execute_trade"' xclaw_finance/logs/*.log | jq '.timestamp, .agent_id, .status'
```

### Errors Only

```bash
grep '"level": "ERROR"' xclaw_finance/logs/*.log | jq '.message, .error, .exception.type'
```

### By Latency

```bash
# Find slow requests (>1000ms)
grep '"latency_ms"' xclaw_finance/logs/*.log | jq 'select(.latency_ms > 1000)'
```

---

## Testing Logging Integration

### Unit Test Example

```python
import pytest
from logging import get_logger, LogLevel, setup_logging

def test_structured_logging():
    setup_logging(level=LogLevel.DEBUG)
    logger = get_logger("test")

    # Log with all fields
    logger.info(
        "Test message",
        request_id="req_test123",
        agent_id="test_agent",
        action="test_action",
        status="success",
        latency_ms=42.5,
    )

    # Verify JSON structure
    # (In real test, capture and parse stderr)
```

### Integration Test Example

```python
@pytest.mark.asyncio
async def test_execute_trade_logging(client):
    response = await client.post(
        "/execute",
        headers={"X-API-Key": "test_key"},
        json={
            "agent_id": "test_agent",
            "wallet_id": "w1",
            "side": "buy",
            "asset": "BTC",
            "amount": "0.1",
        }
    )

    # Verify response contains/references request_id
    # (Optional: include request_id in response body)
    assert response.status_code in (200, 202, 403, 400)
```

---

## Performance Considerations

### Logging Overhead

- **JSON formatting:** ~0.1ms per log entry
- **File I/O:** Async; non-blocking
- **Request tracking:** ~0.2ms per request (ID generation + storage)

### Log Volume

- **Per-trade:** ~5-10 log entries (start, policy, risk, approval, result)
- **Per-request:** ~2-3 log entries (incoming, handler, response)
- **At 1000 trades/hour:** ~10-15 MB/day (with full metadata)

### Recommendations

- Use log rotation (e.g., daily, max 500MB per file)
- Consider sampling for DEBUG level in production
- Archive logs older than 30 days
- Index by request_id in log aggregation system (ELK, CloudWatch, etc.)

---

## Error Categorization

The system automatically categorizes errors:

| Category | Trigger | HTTP Status |
|---|---|---|
| `validation_error` | Pydantic validation | 422 |
| `authentication_error` | Missing/invalid API key | 401 |
| `authorization_error` | Insufficient permissions | 403 |
| `not_found_error` | Resource not found | 404 |
| `conflict_error` | Duplicate/state conflict | 409 |
| `timeout_error` | Request timeout | 504 |
| `rate_limit_error` | Rate limit exceeded | 429 |
| `execution_error` | Business logic rejection | 400/403 |
| `external_service_error` | Exchange API failure | 502 |
| `internal_error` | Uncaught exception | 500 |

---

## Next Steps

1. **Update remaining routes** - Follow `/execute.py` pattern for all route files
2. **Test integration** - Run failure_test.py and verify logs capture failures
3. **Setup log aggregation** - Configure ELK, CloudWatch, or Datadog
4. **Monitor metrics** - Track latency, error rates, request volume
5. **Create runbooks** - Document common debugging scenarios

---

## Debugging with Logs

### Scenario: Trade never executed

```bash
# Find the request
grep "req_12345abc" logs/*.log

# Should see:
# 1. Request logged (incoming)
# 2. Trade logged (handler start)
# 3. Policy decision logged
# 4. Risk decision logged
# 5. Approval decision or execution logged
# 6. Response logged (with status)

# If missing step 5: stuck in approval queue
# If missing step 3: policy engine error
```

### Scenario: High latency request

```bash
# Find slow requests
grep '"latency_ms"' logs/*.log | jq 'select(.latency_ms > 1000)'

# Check each component's latency:
# - "Incoming request" to "Response sent" = total
# - Individual engine calls from metadata
```

### Scenario: Authentication failure

```bash
grep '"level": "ERROR"' logs/*.log | \
grep '"error": "Invalid or inactive API key"' | \
jq '.request_id, .agent_id, .timestamp'
```

---

## Configuration

### Log Levels

Set via `setup_logging(level=LogLevel.X)`:

- **DEBUG** - Detailed internal flow (development only)
- **INFO** - Business events and decisions (production)
- **WARN** - Authorization failures, limits (production)
- **ERROR** - Exceptions and failures (production)

### Log File Rotation

Create in production deployment:

```python
import logging.handlers

handler = logging.handlers.RotatingFileHandler(
    'xclaw_finance/logs/app.log',
    maxBytes=500_000_000,  # 500MB
    backupCount=10,         # keep 10 files
)
```

---

## See Also

- `failure_test.py` - Chaos tests verify error logging
- `LOGGING_INTEGRATION_GUIDE.md` - This file
- `xclaw_finance/logging/` - Logger implementation
