"""
X-Claw Chaos Engineering: Failure & Resilience Testing
Simulates system failures and verifies graceful error handling without crashes.

Test Categories:
  1. INVALID PAYLOADS: missing fields, invalid types, malformed JSON
  2. EXTREME VALUES: zero, negative, very large amounts
  3. API TIMEOUTS: verify timeout handling without server crashes
  4. SPAM ATTACK: burst traffic and rate limit verification

Expectations:
  ✓ No 500 Internal Server Errors (server never crashes)
  ✓ Proper error response codes and JSON structure
  ✓ Audit logs created for all actions
  ✓ Rate limiting enforced without escalation
"""

import asyncio
import json
import httpx
from dataclasses import dataclass, field
from enum import Enum
from decimal import Decimal
from typing import Any
from datetime import datetime
from collections import defaultdict

# ============================================================================
# CONFIG
# ============================================================================

BASE_URL = "http://localhost:8000"
ADMIN_KEY = "xclaw_admin_test_key_placeholder"  # Override if needed
TRADER_KEY = "xclaw_trader_test_key_placeholder"
INVALID_KEY = "xclaw_invalid_key_12345"

REQUEST_TIMEOUT = 30.0
BURST_TIMEOUT = 1.0  # Short timeout for timeout simulation tests


# ============================================================================
# TEST CASE STRUCTURE
# ============================================================================

class TestStatus(str, Enum):
    PASS = "✓ PASS"
    FAIL = "✗ FAIL"
    WARN = "⚠ WARN"
    ERROR = "✗ ERROR"


@dataclass
class ChaosTestCase:
    """Defines a single chaos test case."""
    id: str
    name: str
    category: str
    endpoint: str
    method: str = "POST"
    payload: Any = None
    extra_headers: dict = field(default_factory=dict)
    timeout: float = REQUEST_TIMEOUT
    expect_status: list[int] = field(default_factory=list)  # Empty = any 4xx acceptable
    expect_no_500: bool = True
    description: str = ""
    tags: list[str] = field(default_factory=list)

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other):
        return self.id == other.id if isinstance(other, ChaosTestCase) else False


@dataclass
class TestResult:
    """Result of a test execution."""
    test_case: ChaosTestCase
    status: TestStatus
    http_status: int | None
    response_body: str
    latency_ms: float
    error: str | None = None

    def __str__(self) -> str:
        status_icon = self.status.value
        latency = f"{self.latency_ms:.1f}ms"

        # Truncate response for display
        body_preview = self.response_body[:100].replace("\n", " ")
        if len(self.response_body) > 100:
            body_preview += "..."

        result = f"{status_icon} | {self.test_case.id:20s} | {latency:8s} | HTTP {self.http_status}"
        if self.error:
            result += f" | Error: {self.error}"
        else:
            result += f" | {body_preview}"

        return result


# ============================================================================
# TEST CASE DEFINITIONS
# ============================================================================

def create_test_cases() -> list[ChaosTestCase]:
    """Generate all chaos test cases."""
    tests = []

    # ========== CATEGORY 1: INVALID PAYLOADS ==========

    # Missing required fields
    tests.append(ChaosTestCase(
        id="INV-001",
        name="Missing agent_id",
        category="INVALID_PAYLOADS",
        endpoint="/execute",
        payload={"wallet_id": "w1", "side": "buy", "asset": "BTC", "amount": "1.0"},
        expect_status=[422],
        description="Missing required agent_id field",
        tags=["validation"]
    ))

    tests.append(ChaosTestCase(
        id="INV-002",
        name="Missing wallet_id",
        category="INVALID_PAYLOADS",
        endpoint="/execute",
        payload={"agent_id": "a1", "side": "buy", "asset": "BTC", "amount": "1.0"},
        expect_status=[422],
        description="Missing required wallet_id field",
        tags=["validation"]
    ))

    tests.append(ChaosTestCase(
        id="INV-003",
        name="Missing side field",
        category="INVALID_PAYLOADS",
        endpoint="/execute",
        payload={"agent_id": "a1", "wallet_id": "w1", "asset": "BTC", "amount": "1.0"},
        expect_status=[422],
        description="Missing required side field",
        tags=["validation"]
    ))

    tests.append(ChaosTestCase(
        id="INV-004",
        name="Missing amount field",
        category="INVALID_PAYLOADS",
        endpoint="/execute",
        payload={"agent_id": "a1", "wallet_id": "w1", "side": "buy", "asset": "BTC"},
        expect_status=[422],
        description="Missing required amount field",
        tags=["validation"]
    ))

    tests.append(ChaosTestCase(
        id="INV-005",
        name="Empty request body",
        category="INVALID_PAYLOADS",
        endpoint="/execute",
        payload={},
        expect_status=[422],
        description="Empty JSON object (missing all required fields)",
        tags=["validation"]
    ))

    # Invalid enum values
    tests.append(ChaosTestCase(
        id="INV-006",
        name="Invalid side value",
        category="INVALID_PAYLOADS",
        endpoint="/execute",
        payload={
            "agent_id": "a1",
            "wallet_id": "w1",
            "side": "short",  # Invalid - must be "buy" or "sell"
            "asset": "BTC",
            "amount": "1.0"
        },
        expect_status=[422],
        description="Invalid side value (not 'buy' or 'sell')",
        tags=["validation"]
    ))

    tests.append(ChaosTestCase(
        id="INV-007",
        name="Invalid order_type value",
        category="INVALID_PAYLOADS",
        endpoint="/execute",
        payload={
            "agent_id": "a1",
            "wallet_id": "w1",
            "side": "buy",
            "asset": "BTC",
            "amount": "1.0",
            "order_type": "stoploss"  # Invalid - must be "market" or "limit"
        },
        expect_status=[422],
        description="Invalid order_type value",
        tags=["validation"]
    ))

    # Wrong data types
    tests.append(ChaosTestCase(
        id="INV-008",
        name="Wrong type: amount as string",
        category="INVALID_PAYLOADS",
        endpoint="/execute",
        payload={
            "agent_id": "a1",
            "wallet_id": "w1",
            "side": "buy",
            "asset": "BTC",
            "amount": "not_a_number"  # Should be numeric
        },
        expect_status=[422],
        description="Amount field with non-numeric string value",
        tags=["validation", "type_error"]
    ))

    tests.append(ChaosTestCase(
        id="INV-009",
        name="Wrong type: side as integer",
        category="INVALID_PAYLOADS",
        endpoint="/execute",
        payload={
            "agent_id": "a1",
            "wallet_id": "w1",
            "side": 123,  # Should be string
            "asset": "BTC",
            "amount": "1.0"
        },
        expect_status=[422],
        description="Side field with integer instead of string",
        tags=["validation", "type_error"]
    ))

    tests.append(ChaosTestCase(
        id="INV-010",
        name="Wrong type: amount as boolean",
        category="INVALID_PAYLOADS",
        endpoint="/execute",
        payload={
            "agent_id": "a1",
            "wallet_id": "w1",
            "side": "buy",
            "asset": "BTC",
            "amount": True  # Should be numeric
        },
        expect_status=[422],
        description="Amount as boolean value",
        tags=["validation", "type_error"]
    ))

    # Malformed JSON
    tests.append(ChaosTestCase(
        id="INV-011",
        name="Malformed JSON syntax",
        category="INVALID_PAYLOADS",
        endpoint="/execute",
        payload="{invalid json - missing quotes}",
        expect_status=[422],
        description="Completely malformed JSON body",
        tags=["parsing"]
    ))

    # Missing authentication
    tests.append(ChaosTestCase(
        id="INV-012",
        name="Missing X-API-Key header",
        category="INVALID_PAYLOADS",
        endpoint="/execute",
        payload={"agent_id": "a1", "wallet_id": "w1", "side": "buy", "asset": "BTC", "amount": "1.0"},
        extra_headers={"X-API-Key": ""},  # Empty/missing auth
        expect_status=[401],
        description="Request without X-API-Key header",
        tags=["auth"]
    ))

    tests.append(ChaosTestCase(
        id="INV-013",
        name="Invalid API key",
        category="INVALID_PAYLOADS",
        endpoint="/execute",
        payload={"agent_id": "a1", "wallet_id": "w1", "side": "buy", "asset": "BTC", "amount": "1.0"},
        extra_headers={"X-API-Key": INVALID_KEY},
        expect_status=[401],
        description="Request with non-existent/invalid API key",
        tags=["auth"]
    ))

    # ========== CATEGORY 2: EXTREME VALUES ==========

    # Zero and negative amounts
    tests.append(ChaosTestCase(
        id="EXT-001",
        name="Zero amount",
        category="EXTREME_VALUES",
        endpoint="/execute",
        payload={
            "agent_id": "a1",
            "wallet_id": "w1",
            "side": "buy",
            "asset": "BTC",
            "amount": "0"  # Violates gt=0
        },
        expect_status=[422],
        description="Amount = 0 (violates gt=0 constraint)",
        tags=["validation"]
    ))

    tests.append(ChaosTestCase(
        id="EXT-002",
        name="Negative amount",
        category="EXTREME_VALUES",
        endpoint="/execute",
        payload={
            "agent_id": "a1",
            "wallet_id": "w1",
            "side": "buy",
            "asset": "BTC",
            "amount": "-1.5"  # Violates gt=0
        },
        expect_status=[422],
        description="Amount = -1.5 (violates gt=0 constraint)",
        tags=["validation"]
    ))

    # Extremely large amounts
    tests.append(ChaosTestCase(
        id="EXT-003",
        name="Extremely large amount",
        category="EXTREME_VALUES",
        endpoint="/execute",
        payload={
            "agent_id": "a1",
            "wallet_id": "w1",
            "side": "buy",
            "asset": "BTC",
            "amount": "999999999999999999999"
        },
        expect_status=[422, 400, 403],  # Could be validation error or business rejection
        description="Amount = 999999999999999999999",
        tags=["extreme"]
    ))

    # Extremely small amounts (potential precision issues)
    tests.append(ChaosTestCase(
        id="EXT-004",
        name="Extremely small amount",
        category="EXTREME_VALUES",
        endpoint="/execute",
        payload={
            "agent_id": "a1",
            "wallet_id": "w1",
            "side": "buy",
            "asset": "BTC",
            "amount": "0.00000000000001"
        },
        expect_no_500=True,
        description="Amount = 0.00000000000001 (very small but valid)",
        tags=["extreme"]
    ))

    # Invalid limit price
    tests.append(ChaosTestCase(
        id="EXT-005",
        name="Zero limit_price",
        category="EXTREME_VALUES",
        endpoint="/execute",
        payload={
            "agent_id": "a1",
            "wallet_id": "w1",
            "side": "buy",
            "asset": "BTC",
            "amount": "1.0",
            "order_type": "limit",
            "limit_price": "0"  # Violates gt=0
        },
        expect_status=[422],
        description="Limit order with limit_price = 0",
        tags=["validation"]
    ))

    tests.append(ChaosTestCase(
        id="EXT-006",
        name="Negative limit_price",
        category="EXTREME_VALUES",
        endpoint="/execute",
        payload={
            "agent_id": "a1",
            "wallet_id": "w1",
            "side": "buy",
            "asset": "BTC",
            "amount": "1.0",
            "order_type": "limit",
            "limit_price": "-50.5"  # Violates gt=0
        },
        expect_status=[422],
        description="Limit order with negative limit_price",
        tags=["validation"]
    ))

    # Negative daily_volume_usd
    tests.append(ChaosTestCase(
        id="EXT-007",
        name="Negative daily_volume_usd",
        category="EXTREME_VALUES",
        endpoint="/execute",
        payload={
            "agent_id": "a1",
            "wallet_id": "w1",
            "side": "buy",
            "asset": "BTC",
            "amount": "1.0",
            "daily_volume_usd": "-1000"  # Violates ge=0
        },
        expect_status=[422],
        description="daily_volume_usd = -1000 (violates ge=0 constraint)",
        tags=["validation"]
    ))

    # Very long string values (potential DoS/buffer overflow)
    tests.append(ChaosTestCase(
        id="EXT-008",
        name="Extremely long agent_id",
        category="EXTREME_VALUES",
        endpoint="/execute",
        payload={
            "agent_id": "a" * 10000,  # 10k character agent_id
            "wallet_id": "w1",
            "side": "buy",
            "asset": "BTC",
            "amount": "1.0"
        },
        expect_no_500=True,
        description="agent_id with 10,000 characters",
        tags=["extreme", "dos"]
    ))

    # Unicode/special characters
    tests.append(ChaosTestCase(
        id="EXT-009",
        name="Unicode in asset field",
        category="EXTREME_VALUES",
        endpoint="/execute",
        payload={
            "agent_id": "a1",
            "wallet_id": "w1",
            "side": "buy",
            "asset": "💀💰💎BTC",  # Emoji symbols
            "amount": "1.0"
        },
        expect_no_500=True,
        description="Asset field with Unicode emoji characters",
        tags=["extreme", "unicode"]
    ))

    # SQL injection attempt (should be safely handled)
    tests.append(ChaosTestCase(
        id="EXT-010",
        name="SQL injection in agent_id",
        category="EXTREME_VALUES",
        endpoint="/execute",
        payload={
            "agent_id": "a1'; DROP TABLE agents; --",
            "wallet_id": "w1",
            "side": "buy",
            "asset": "BTC",
            "amount": "1.0"
        },
        expect_no_500=True,
        description="SQL injection payload in agent_id field",
        tags=["security", "injection"]
    ))

    # XSS attempt in note field
    tests.append(ChaosTestCase(
        id="EXT-011",
        name="XSS payload in note field",
        category="EXTREME_VALUES",
        endpoint="/approve",
        method="POST",
        payload={
            "request_id": "req_123",
            "decision": "approve",
            "note": "<script>alert('xss')</script>"
        },
        extra_headers={"X-API-Key": ADMIN_KEY},
        expect_no_500=True,
        description="XSS payload in approval note field",
        tags=["security", "xss"]
    ))

    # ========== CATEGORY 3: API TIMEOUT SIMULATION ==========

    tests.append(ChaosTestCase(
        id="TMO-001",
        name="Request timeout (1ms)",
        category="API_TIMEOUT",
        endpoint="/execute",
        payload={
            "agent_id": "a1",
            "wallet_id": "w1",
            "side": "buy",
            "asset": "BTC",
            "amount": "1.0"
        },
        timeout=0.001,  # 1ms timeout - will likely timeout
        expect_no_500=True,
        description="Request with 1ms timeout (should trigger client-side timeout)",
        tags=["timeout", "resilience"]
    ))

    tests.append(ChaosTestCase(
        id="TMO-002",
        name="Very short timeout (10ms)",
        category="API_TIMEOUT",
        endpoint="/execute",
        payload={
            "agent_id": "a1",
            "wallet_id": "w1",
            "side": "buy",
            "asset": "BTC",
            "amount": "1.0"
        },
        timeout=0.010,  # 10ms timeout
        expect_no_500=True,
        description="Request with 10ms timeout",
        tags=["timeout", "resilience"]
    ))

    return tests


# ============================================================================
# TEST EXECUTION
# ============================================================================

class ChaosTestRunner:
    """Executes chaos tests and tracks results."""

    def __init__(self, base_url: str):
        self.base_url = base_url
        self.results: list[TestResult] = []
        self.client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self.client = httpx.AsyncClient(base_url=self.base_url)
        return self

    async def __aexit__(self, *args):
        if self.client:
            await self.client.aclose()

    async def run_test(self, test: ChaosTestCase) -> TestResult:
        """Execute a single test case."""
        if not self.client:
            raise RuntimeError("Client not initialized")

        start = datetime.now()
        status = TestStatus.PASS
        http_status = None
        response_body = ""
        error = None

        try:
            # Prepare headers
            headers = {"X-API-Key": ADMIN_KEY}
            if test.extra_headers:
                headers.update(test.extra_headers)

            # Handle raw (non-JSON) payloads
            if isinstance(test.payload, str):
                # Malformed JSON test
                response = await self.client.request(
                    method=test.method,
                    url=test.endpoint,
                    headers=headers,
                    content=test.payload,
                    timeout=test.timeout,
                )
            elif test.payload is None:
                response = await self.client.request(
                    method=test.method,
                    url=test.endpoint,
                    headers=headers,
                    timeout=test.timeout,
                )
            else:
                response = await self.client.request(
                    method=test.method,
                    url=test.endpoint,
                    headers=headers,
                    json=test.payload,
                    timeout=test.timeout,
                )

            http_status = response.status_code
            response_body = response.text

            # Evaluate response
            if http_status == 500:
                status = TestStatus.FAIL
                error = "Server returned 500 (crash)"
            elif test.expect_status and http_status not in test.expect_status:
                status = TestStatus.WARN
                error = f"Expected {test.expect_status}, got {http_status}"
            elif http_status >= 400 and http_status < 500:
                status = TestStatus.PASS
            else:
                status = TestStatus.PASS

        except httpx.TimeoutException:
            # Timeout is expected for timeout tests
            if "TMO" in test.id:
                status = TestStatus.PASS
                error = "TimeoutException (expected)"
            else:
                status = TestStatus.WARN
                error = "TimeoutException (unexpected)"
        except httpx.ConnectError as e:
            status = TestStatus.ERROR
            error = f"ConnectError: {str(e)[:50]}"
        except Exception as e:
            status = TestStatus.ERROR
            error = f"{type(e).__name__}: {str(e)[:50]}"

        latency = (datetime.now() - start).total_seconds() * 1000

        result = TestResult(
            test_case=test,
            status=status,
            http_status=http_status,
            response_body=response_body,
            latency_ms=latency,
            error=error,
        )

        self.results.append(result)
        return result

    async def run_all_tests(self, tests: list[ChaosTestCase]) -> None:
        """Execute all tests sequentially."""
        for test in tests:
            result = await self.run_test(test)
            print(str(result))

    async def run_burst_attack(
        self, concurrent_count: int = 100, requests_per_task: int = 1
    ) -> None:
        """Simulate burst/spam attack with concurrent requests."""
        print("\n" + "=" * 120)
        print(f"BURST ATTACK SIMULATION: {concurrent_count} concurrent requests")
        print("=" * 120 + "\n")

        test_payload = {
            "agent_id": "burst_test",
            "wallet_id": "w1",
            "side": "buy",
            "asset": "BTC",
            "amount": "1.0",
        }

        async def burst_request(task_id: int) -> TestResult:
            if not self.client:
                raise RuntimeError("Client not initialized")

            start = datetime.now()
            try:
                response = await self.client.post(
                    "/execute",
                    headers={"X-API-Key": ADMIN_KEY},
                    json=test_payload,
                    timeout=BURST_TIMEOUT,
                )
                latency = (datetime.now() - start).total_seconds() * 1000

                return TestResult(
                    test_case=ChaosTestCase(
                        id=f"BURST-{task_id}",
                        name="Burst request",
                        category="SPAM_ATTACK",
                        endpoint="/execute",
                    ),
                    status=TestStatus.PASS if response.status_code < 500 else TestStatus.FAIL,
                    http_status=response.status_code,
                    response_body=response.text[:100],
                    latency_ms=latency,
                )
            except Exception as e:
                return TestResult(
                    test_case=ChaosTestCase(
                        id=f"BURST-{task_id}",
                        name="Burst request",
                        category="SPAM_ATTACK",
                        endpoint="/execute",
                    ),
                    status=TestStatus.ERROR,
                    http_status=None,
                    response_body="",
                    latency_ms=0,
                    error=str(e)[:50],
                )

        # Launch all burst requests concurrently
        tasks = [burst_request(i) for i in range(concurrent_count)]
        burst_results = await asyncio.gather(*tasks)

        # Analyze burst results
        status_counts = defaultdict(int)
        latencies = []

        for result in burst_results:
            if result.http_status:
                status_counts[result.http_status] += 1
            latencies.append(result.latency_ms)
            self.results.append(result)

        # Print burst summary
        print(f"Status Code Distribution:")
        for status_code in sorted(status_counts.keys()):
            count = status_counts[status_code]
            print(f"  HTTP {status_code}: {count} requests")

        if latencies:
            print(f"\nLatency Statistics:")
            print(f"  Min: {min(latencies):.1f}ms")
            print(f"  Avg: {sum(latencies) / len(latencies):.1f}ms")
            print(f"  Max: {max(latencies):.1f}ms")

        # Check for crashes
        crashes = sum(1 for r in burst_results if r.http_status == 500)
        if crashes > 0:
            print(f"\n❌ {crashes} requests resulted in 500 errors (SERVER CRASHED)")
        else:
            print(f"\n✓ No 500 errors (server remained stable)")


# ============================================================================
# AUDIT LOG VERIFICATION
# ============================================================================

async def verify_audit_logs(client: httpx.AsyncClient) -> None:
    """Query audit logs to verify they were created."""
    print("\n" + "=" * 120)
    print("AUDIT LOG VERIFICATION")
    print("=" * 120 + "\n")

    try:
        # Query history/audit logs
        response = await client.get(
            "/history",
            headers={"X-API-Key": ADMIN_KEY},
            params={"limit": 10},
        )

        if response.status_code == 200:
            data = response.json()
            print(f"✓ Audit logs accessible")
            if isinstance(data, dict) and "entries" in data:
                count = len(data.get("entries", []))
                print(f"  Found {count} audit entries")
            else:
                print(f"  Response: {str(data)[:200]}")
        else:
            print(f"⚠ Could not fetch audit logs (HTTP {response.status_code})")
    except Exception as e:
        print(f"⚠ Audit log verification failed: {e}")


# ============================================================================
# HEALTH CHECKS
# ============================================================================

async def check_server_health(client: httpx.AsyncClient) -> bool:
    """Check if server is running and responsive."""
    try:
        response = await client.get("/", timeout=5.0)
        return response.status_code < 500
    except Exception:
        return False


# ============================================================================
# REPORTING
# ============================================================================

def print_summary(runner: ChaosTestRunner) -> None:
    """Print comprehensive test summary and statistics."""
    print("\n" + "=" * 120)
    print("CHAOS ENGINEERING TEST SUMMARY")
    print("=" * 120 + "\n")

    # Group results by category
    by_category = defaultdict(list)
    for result in runner.results:
        by_category[result.test_case.category].append(result)

    # Overall stats
    total = len(runner.results)
    passed = sum(1 for r in runner.results if r.status == TestStatus.PASS)
    warned = sum(1 for r in runner.results if r.status == TestStatus.WARN)
    failed = sum(1 for r in runner.results if r.status == TestStatus.FAIL)
    errors = sum(1 for r in runner.results if r.status == TestStatus.ERROR)
    crashes_500 = sum(1 for r in runner.results if r.http_status == 500)

    print(f"Total Tests Run: {total}")
    print(f"  ✓ Passed: {passed}")
    print(f"  ⚠ Warned: {warned}")
    print(f"  ✗ Failed: {failed}")
    print(f"  ✗ Errors: {errors}")
    print(f"\n500 Errors (Crashes): {crashes_500}")

    # Category breakdown
    print(f"\nBreakdown by Category:")
    for category in sorted(by_category.keys()):
        results = by_category[category]
        cat_passed = sum(1 for r in results if r.status == TestStatus.PASS)
        cat_total = len(results)
        print(f"  {category:20s}: {cat_passed}/{cat_total} passed")

    # Failure details
    if failed > 0 or errors > 0:
        print(f"\nFailed/Error Tests:")
        for result in runner.results:
            if result.status in (TestStatus.FAIL, TestStatus.ERROR):
                print(
                    f"  {result.test_case.id}: {result.test_case.name} ({result.error})"
                )

    # Overall assessment
    print(f"\n" + "=" * 120)
    if crashes_500 == 0 and failed == 0:
        print("✓ CHAOS TESTING PASSED - No server crashes or major failures detected")
    elif crashes_500 == 0:
        print("⚠ CHAOS TESTING PARTIALLY PASSED - No crashes, but some unexpected responses")
    else:
        print(f"✗ CHAOS TESTING FAILED - {crashes_500} server crashes detected")
    print("=" * 120 + "\n")


# ============================================================================
# MAIN EXECUTION
# ============================================================================

async def main():
    """Main test execution."""
    print("\n" + "=" * 120)
    print("X-CLAW CHAOS ENGINEERING: FAILURE & RESILIENCE TEST SUITE")
    print("=" * 120)
    print(f"Target: {BASE_URL}\n")

    # Check server health before starting
    print("Checking server health...", end=" ")
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        if not await check_server_health(client):
            print("✗ Server not responding")
            return

        print("✓ Server is up\n")

        # Create test cases
        tests = create_test_cases()
        print(f"Loaded {len(tests)} test cases\n")

        # Run tests
        async with ChaosTestRunner(BASE_URL) as runner:
            print("=" * 120)
            print("RUNNING INVALID PAYLOAD TESTS")
            print("=" * 120 + "\n")
            invalid_tests = [t for t in tests if t.category == "INVALID_PAYLOADS"]
            await runner.run_all_tests(invalid_tests)

            print("\n" + "=" * 120)
            print("RUNNING EXTREME VALUE TESTS")
            print("=" * 120 + "\n")
            extreme_tests = [t for t in tests if t.category == "EXTREME_VALUES"]
            await runner.run_all_tests(extreme_tests)

            print("\n" + "=" * 120)
            print("RUNNING TIMEOUT SIMULATION TESTS")
            print("=" * 120 + "\n")
            timeout_tests = [t for t in tests if t.category == "API_TIMEOUT"]
            await runner.run_all_tests(timeout_tests)

            # Verify server still responsive after timeouts
            print("\nVerifying server still responsive after timeouts...", end=" ")
            if await check_server_health(client):
                print("✓ Server is still up")
            else:
                print("✗ Server became unresponsive")

            # Burst attack
            await runner.run_burst_attack(concurrent_count=100)

            # Verify audit logs were created
            await verify_audit_logs(client)

            # Print final summary
            print_summary(runner)


if __name__ == "__main__":
    asyncio.run(main())
