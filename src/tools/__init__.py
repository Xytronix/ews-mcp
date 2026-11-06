"""MCP Tools for EWS operations."""

# Exchange-only email tools. The mail-CRUD tools that serve every provider
# (send/read/search/get/reply/move/delete/update + list_folders) are imported
# from .mailbox_tools below — those subclass the Exchange implementations and
# keep the same public tool names.
from .email_tools import GetEmailsBulkTool, CopyEmailTool
from .graph_calendar_tools import CreateAppointmentTool, GetCalendarTool, UpdateAppointmentTool, DeleteAppointmentTool, RespondToMeetingTool, CheckAvailabilityTool, FindMeetingTimesTool
from .graph_contact_tools import CreateContactTool, UpdateContactTool, DeleteContactTool, FindPersonTool, AnalyzeContactsTool
from .task_tools import CreateTaskTool, GetTasksTool, UpdateTaskTool, CompleteTaskTool, DeleteTaskTool
from .search_tools import SearchByConversationTool
from .folder_tools import FindFolderTool, ManageFolderTool
from .oof_tools import OofSettingsTool
from .ai_tools import SemanticSearchEmailsTool

# Agent-secretary tools (memory, commitments, approvals, rules, voice, OOF
# policy, briefing, meeting prep).
from .memory_tools import MemorySetTool, MemoryGetTool, MemoryListTool, MemoryDeleteTool
from .commitment_tools import (
    TrackCommitmentTool,
    ListCommitmentsTool,
    ResolveCommitmentTool,
)
from .approval_tools import (
    SubmitForApprovalTool,
    ListPendingApprovalsTool,
    ApproveTool,
    RejectTool,
    ExecuteApprovedActionTool,
)
from .voice_tools import GetVoiceProfileTool
from .rule_tools import (
    RuleCreateTool,
    RuleListTool,
    RuleDeleteTool,
    RuleSimulateTool,
    EvaluateRulesOnMessageTool,
)
from .oof_policy_tools import (
    ConfigureOOFPolicyTool,
    GetOOFPolicyTool,
    ApplyOOFPolicyTool,
)
from .briefing_tools import GenerateBriefingTool
from .meeting_prep_tools import PrepareMeetingTool

# One mail-CRUD + attachment surface for every configured mailbox: these
# subclass the Exchange tools and route to the IMAP (iCloud) / Graph (Outlook)
# backend when the addressed mailbox is served by one. Same tool names, no
# duplicates.
from .mailbox_tools import (
    CreateDraftTool,
    CreateReplyDraftTool,
    CreateForwardDraftTool,
    UpdateDraftTool,
    ListMailboxesTool,
    ListFoldersTool,
    ReadEmailsTool,
    SearchEmailsTool,
    GetEmailDetailsTool,
    SendEmailTool,
    ReplyEmailTool,
    ForwardEmailTool,
    MoveEmailTool,
    DeleteEmailTool,
    UpdateEmailTool,
    ListAttachmentsTool,
    DownloadAttachmentTool,
    AddAttachmentTool,
    DeleteAttachmentTool,
    ReadAttachmentTool,
    GetEmailMimeTool,
    AttachEmailToDraftTool,
)
from .staging_tools import StageOutgoingFileTool, DeleteStagedFileTool

__all__ = [
    # Email tools (11)
    "CreateDraftTool",
    "CreateReplyDraftTool",
    "CreateForwardDraftTool",
    "UpdateDraftTool",
    "SendEmailTool",
    "ReadEmailsTool",
    "SearchEmailsTool",
    "GetEmailDetailsTool",
    "GetEmailsBulkTool",
    "DeleteEmailTool",
    "MoveEmailTool",
    "UpdateEmailTool",
    "CopyEmailTool",
    "ReplyEmailTool",
    "ForwardEmailTool",
    # Mailbox inventory (1)
    "ListMailboxesTool",
    # Calendar tools (7)
    "CreateAppointmentTool",
    "GetCalendarTool",
    "UpdateAppointmentTool",
    "DeleteAppointmentTool",
    "RespondToMeetingTool",
    "CheckAvailabilityTool",
    "FindMeetingTimesTool",
    # Contact tools (3)
    "CreateContactTool",
    "UpdateContactTool",
    "DeleteContactTool",
    # Task tools (5)
    "CreateTaskTool",
    "GetTasksTool",
    "UpdateTaskTool",
    "CompleteTaskTool",
    "DeleteTaskTool",
    # Attachment tools (9, incl. outbound staging)
    "ListAttachmentsTool",
    "DownloadAttachmentTool",
    "AddAttachmentTool",
    "DeleteAttachmentTool",
    "ReadAttachmentTool",
    "GetEmailMimeTool",
    "AttachEmailToDraftTool",
    "StageOutgoingFileTool",
    "DeleteStagedFileTool",
    # Search tools (1)
    "SearchByConversationTool",
    # Folder tools (3)
    "ListFoldersTool",
    "FindFolderTool",
    "ManageFolderTool",
    # Out-of-Office tools (1)
    "OofSettingsTool",
    # AI tools (1 in v4 — semantic only; classification/summarisation/
    # reply-suggest moved to skill side)
    "SemanticSearchEmailsTool",
    # Contact Intelligence tools (2)
    "FindPersonTool",
    "AnalyzeContactsTool",
    # --- Agent-secretary tools ---
    # Memory (4)
    "MemorySetTool",
    "MemoryGetTool",
    "MemoryListTool",
    "MemoryDeleteTool",
    # Commitments (3 in v4 — manual CRUD only; auto-extraction moved to skill)
    "TrackCommitmentTool",
    "ListCommitmentsTool",
    "ResolveCommitmentTool",
    # Approvals (5)
    "SubmitForApprovalTool",
    "ListPendingApprovalsTool",
    "ApproveTool",
    "RejectTool",
    "ExecuteApprovedActionTool",
    # Voice profile (1 in v4 — read-only; tone analysis moved to skill side)
    "GetVoiceProfileTool",
    # Rule engine (5)
    "RuleCreateTool",
    "RuleListTool",
    "RuleDeleteTool",
    "RuleSimulateTool",
    "EvaluateRulesOnMessageTool",
    # OOF policy (3)
    "ConfigureOOFPolicyTool",
    "GetOOFPolicyTool",
    "ApplyOOFPolicyTool",
    # Compound tools (2)
    "GenerateBriefingTool",
    "PrepareMeetingTool",
]
