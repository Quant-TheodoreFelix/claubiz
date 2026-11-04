from dataclasses import dataclass, field

from anthropic import AsyncAnthropic

from .audit import AuditLog
from .budget import Ledger, ReservationPool
from .config import Settings, load_settings
from .events import EventBus
from .llm import LLM
from .orchestrator import Orchestrator
from .secrets import KeychainStore, install_log_redaction, scrub_credential_env
from .transport import build_client
from .workspace import Workspace


@dataclass
class Runtime:
    settings: Settings
    ledger: Ledger
    audit: AuditLog
    bus: EventBus
    client: AsyncAnthropic
    llm: LLM
    pool: ReservationPool = field(default_factory=ReservationPool)

    @classmethod
    def create(cls) -> "Runtime":
        install_log_redaction()
        scrubbed = scrub_credential_env()
        settings = load_settings()
        audit = AuditLog()
        if scrubbed:
            audit.append("env.scrubbed", names=scrubbed)
        store = KeychainStore(settings.api.keychain_service, settings.api.keychain_account)
        key = store.require()
        client = build_client(settings, key)
        ledger = Ledger()
        bus = EventBus()
        llm = LLM(client, settings, ledger, bus, audit)
        audit.append("runtime.started", key_fingerprint=key.fingerprint(), base_url=settings.api.base_url)
        return cls(settings, ledger, audit, bus, client, llm)

    def orchestrator(self, ws: Workspace) -> Orchestrator:
        return Orchestrator(self.settings, ws, self.llm, self.ledger, self.audit, self.bus, self.pool)

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.close()
        self.ledger.close()
