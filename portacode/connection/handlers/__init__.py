"""Modular command handler system for Portacode client.

This package provides a flexible system for handling commands from the gateway.
Handlers can be easily added, removed, or modified without touching the main
terminal manager code.
"""

from .base import BaseHandler, AsyncHandler, SyncHandler
from .registry import CommandRegistry
from .terminal_handlers import (
    TerminalStartHandler,
    TerminalSendHandler,
    TerminalStopHandler,
    TerminalListHandler,
    TerminalExecHandler,
)
from .system_handlers import SystemInfoHandler
from .browser_handlers import BrowserRunHandler
from .update_handler import UpdatePortacodeHandler
from .file_handlers import (
    FileReadHandler,
    ImageReadHandler,
    FileWriteHandler,
    DirectoryListHandler,
    FileInfoHandler,
    FileDeleteHandler,
    FileCreateHandler,
    FolderCreateHandler,
    FileRenameHandler,
    FileMoveCopyHandler,
    FileSearchHandler,
    ContentRequestHandler,
)
from .diff_handlers import FileApplyDiffHandler, FilePreviewDiffHandler
from .resumable_transfer_handlers import (
    TransferPrepareHandler,
    TransferReadChunkHandler,
    TransferReceiveChunkHandler,
    TransferStatusHandler,
    TransferFinalizeHandler,
    TransferCancelHandler,
)
from .project_state_handlers import (
    ProjectStateFolderExpandHandler,
    ProjectStateFolderCollapseHandler,
    ProjectStateFileOpenHandler,
    ProjectStateTabCloseHandler,
    ProjectStateSetActiveTabHandler,
    ProjectStateDiffOpenHandler,
    ProjectStateDiffContentHandler,
    ProjectStateGitStageHandler,
    ProjectStateGitUnstageHandler,
    ProjectStateGitRevertHandler,
    ProjectStateGitCommitHandler,
)
from .proxmox_infra import (
    ConfigureProxmoxInfraHandler,
    CreateProxmoxContainerHandler,
    RevertProxmoxInfraHandler,
    StartPortacodeServiceHandler,
    StartProxmoxContainerHandler,
    StopProxmoxContainerHandler,
    RestartProxmoxContainerHandler,
    RemoveProxmoxContainerHandler,
)
from .cloudflare_tunnel import CloudflareTunnelSetupHandler
from .cloudflare_forwarding import (
    CloudflareForwardingHandler,
    ConfigureProxmoxContainerExposePortsHandler,
)
from .automation_v2_handlers import (
    AutomationV2StartHandler,
    AutomationV2StateHandler,
    AutomationV2CancelHandler,
)
from .codex_handlers import (
    CodexStatusHandler,
    CodexThreadListHandler,
    CodexThreadStartHandler,
    CodexThreadResumeHandler,
    CodexTurnStartHandler,
    CodexTurnInterruptHandler,
    CodexTaskExecuteHandler,
    CodexPrepareHandler,
)

__all__ = [
    "BaseHandler",
    "AsyncHandler", 
    "SyncHandler",
    "CommandRegistry",
    "TerminalStartHandler",
    "TerminalSendHandler",
    "TerminalStopHandler",
    "TerminalListHandler",
    "TerminalExecHandler",
    "SystemInfoHandler",
    "BrowserRunHandler",
    "ConfigureProxmoxInfraHandler",
    "CreateProxmoxContainerHandler",
    # File operation handlers (optional - register as needed)
    "FileReadHandler",
    "ImageReadHandler",
    "FileWriteHandler", 
    "DirectoryListHandler",
    "FileInfoHandler",
    "FileDeleteHandler",
    "FileCreateHandler",
    "FolderCreateHandler",
    "FileRenameHandler",
    "FileMoveCopyHandler",
    "FileSearchHandler",
    "ContentRequestHandler",
    "FileApplyDiffHandler",
    "FilePreviewDiffHandler",
    "TransferPrepareHandler",
    "TransferReadChunkHandler",
    "TransferReceiveChunkHandler",
    "TransferStatusHandler",
    "TransferFinalizeHandler",
    "TransferCancelHandler",
    # Project state handlers
    "ProjectStateFolderExpandHandler",
    "ProjectStateFolderCollapseHandler",
    "ProjectStateFileOpenHandler",
    "ProjectStateTabCloseHandler",
    "ProjectStateSetActiveTabHandler",
    "ProjectStateDiffOpenHandler",
    "ProjectStateDiffContentHandler",
    "ProjectStateGitStageHandler",
    "ProjectStateGitUnstageHandler",
    "ProjectStateGitRevertHandler",
    "ProjectStateGitCommitHandler",
    "StartPortacodeServiceHandler",
    "StartProxmoxContainerHandler",
    "StopProxmoxContainerHandler",
    "RestartProxmoxContainerHandler",
    "RemoveProxmoxContainerHandler",
    "UpdatePortacodeHandler",
    "RevertProxmoxInfraHandler",
    "CloudflareTunnelSetupHandler",
    "CloudflareForwardingHandler",
    "ConfigureProxmoxContainerExposePortsHandler",
    "AutomationV2StartHandler",
    "AutomationV2StateHandler",
    "AutomationV2CancelHandler",
    # Codex chat handlers
    "CodexStatusHandler",
    "CodexThreadListHandler",
    "CodexThreadStartHandler",
    "CodexThreadResumeHandler",
    "CodexTurnStartHandler",
    "CodexTurnInterruptHandler",
    "CodexTaskExecuteHandler",
    "CodexPrepareHandler",
]
