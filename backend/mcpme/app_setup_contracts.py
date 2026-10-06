"""Host-owned setup and repair authority; no source-supplied execution tools."""
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import StrictModel
from .preparation_agent_contracts import AnalysisRequestV1, Digest, Identity, Role
from .preparation_contracts import contract_digest


class RepairContextV1(StrictModel):
    schema_version: Literal[1] = 1
    workflow_id: Identity
    prior_attempt_id: Identity
    prior_proposal_digest: Digest
    mode: Literal["complete-proposal", "repair-adapter"]
    roles: Annotated[list[Role], Field(min_length=1, max_length=4)]
    diagnostic_code: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]

    @model_validator(mode="after")
    def fixed_roles(self):
        ordered = [role for role in ("interface", "dependencies", "fixtures", "adapter") if role in self.roles]
        if self.roles != ordered or self.mode == "repair-adapter" and self.roles != ["adapter"]:
            raise ValueError("app_setup_repair_scope_invalid")
        return self


class RepairAnalysisRequestV1(AnalysisRequestV1):
    """Separate digest domain prevents a four-call review becoming repair authority."""
    repair_context: RepairContextV1

    @property
    def digest(self):
        return contract_digest("app-setup-repair-request-v1", self.model_dump())


# Status and provider repair feedback use these fixed messages, never raw
# exception strings, provider output, source text, or process stderr.
DIAGNOSTICS = {
    "agent_candidate_unresolved": ("The generated setup still has missing interface, dependency, or test details.", "Review the app instructions and try Prepare App again."),
    "agent_adapter_proposal_invalid": ("The assistant returned an incomplete adapter contract.", "Try Prepare App again; the assistant must return all required setup files."),
    "agent_adapter_static_eligibility_required": ("The generated adapter contains unsupported Python structure or invalid syntax.", "The assistant must produce one typed invoke function with imports only at module scope."),
    "agent_adapter_interface_mismatch": ("The generated function's arguments do not match its tool schema.", "The assistant must make the wrapper and tool schema agree."),
    "agent_adapter_fixture_mismatch": ("The generated test input or expected result does not match the tool schema.", "The assistant must supply a valid synthetic test before the app can be added."),
    "agent_adapter_original_input_required": ("The setup refers to a source file that is missing from the selected folder.", "Include the required app files in the folder, then select the folder again."),
    "locked_dependency_project_required": ("The dependency project and lock file do not match.", "Update the project's lock file, then select the folder again."),
    "agent_dependency_resolution_required": ("The app's Python dependencies still need to be resolved.", "Try Prepare App again to resolve and test the dependencies."),
    "agent_provider_rejected": ("The configured LLM provider rejected the request.", "Open LLM Providers and use Test Connection to check the key and model."),
    "agent_credentials_unavailable": ("The configured LLM provider credential is unavailable.", "Open LLM Providers and save the API key again."),
    "agent_provider_incomplete": ("The model stopped before returning a complete setup.", "Try again with a model that can return the complete adapter."),
    "agent_provider_output_invalid": ("The model did not return the required structured setup.", "Select a model that supports structured output in LLM Providers."),
    "agent_proposal_invalid": ("The model returned an invalid structured setup.", "Try again or choose a different model in LLM Providers."),
    "agent_provider_failed": ("The LLM provider could not be reached.", "Check the connection using Test Connection in LLM Providers."),
    "agent_provider_response_limit": ("The model's setup response exceeded the size limit.", "Ask for a smaller, focused app interface and try again."),
    "agent_deadline": ("The model did not finish within the setup time limit.", "Try again with a faster model."),
    "agent_transfer_limit": ("The setup reached its reviewed provider transfer limit.", "Use a smaller app folder or narrower instructions and try again."),
    "agent_execution_disabled": ("AI setup is unavailable with the selected provider and this app build.", "Check the provider with Test Connection and use the latest app build."),
    "agent_provider_changed": ("The provider configuration changed while the app was being prepared.", "Start preparation again with the updated provider."),
    "agent_source_identity_mismatch": ("The selected app snapshot changed during preparation.", "Select the folder again and restart preparation."),
    "preparation_runner_unavailable": ("This app build cannot run app preparation.", "Use the signed app build with the preparation runtime included."),
    "resolution_runner_unavailable": ("This app build cannot resolve the app's dependencies.", "Use the signed app build with dependency resolution included."),
    "preparation_fixture_mismatch": ("The app ran, but its result did not match the original test.", "The assistant's repair could not pass the unchanged test. Check the app's expected behavior."),
    "prepared_fixture_mismatch": ("The app ran, but its result did not match the original test.", "The assistant's repair could not pass the unchanged test. Check the app's expected behavior."),
    "preparation_command_failed": ("The app's confined build or test command failed.", "Check that the app supports the bundled Python runtime and its dependencies are available."),
    "preparation_probe_failed": ("The built app did not pass its confined tool test.", "Check the app's required runtime files and expected behavior."),
    "preparation_fixture_failed": ("The app's tool failed its original synthetic behavior test.", "The assistant must fix the adapter while preserving the test input and expected result."),
    "preparation_tool_schema_mismatch": ("The running app exposes a different tool schema from its approved setup.", "The assistant must make the running wrapper match the fixed tool schema."),
    "preparation_probe_timeout": ("The built app did not finish its tool test in time.", "Check the model's startup time and runtime files."),
    "preparation_probe_memory_limit": ("The app exceeded its memory budget while running the tool test.", "Reduce the app's model or runtime memory requirements."),
    "preparation_wall_limit": ("Building or testing the app exceeded the time limit.", "Check whether the app requires a large download or a long-running startup."),
    "preparation_memory_limit": ("Building or testing the app exceeded the memory budget.", "Reduce the app's model or runtime memory requirements."),
    "dependency_resolution_failed": ("The app's Python dependencies could not be resolved.", "Check the dependency declarations and supported Python version."),
    "app_setup_folder_limit": ("This folder has more source text than the assistant can review in one setup.", "Select the app's source directory without unrelated projects or generated files."),
    "app_setup_python_required": ("Automatic setup could not identify a supported Python app in this folder.", "Select the Python app's directory, including its source and dependency files."),
    "app_setup_budget_exhausted": ("The assistant used the reviewed repair attempts without producing a working app.", "Check the reported failing stage, update the app instructions, and try again."),
    "app_setup_deadline": ("The complete app setup exceeded its time limit.", "Try again after checking the app's dependencies and runtime files."),
    "app_setup_changed": ("The saved app setup changed while preparation was running.", "Start Prepare App again with the current folder and instructions."),
    "app_setup_busy": ("This app already has a preparation running.", "Wait for it to finish or cancel it before starting again."),
    "app_setup_restarted": ("The app closed before preparation finished.", "Start Prepare App again. Previous provider requests will not be replayed."),
    "app_setup_ownership_unsettled": ("The preparation worker has not finished shutting down.", "Wait for the worker to stop before starting preparation again."),
    "app_setup_failed": ("The app setup could not complete its current stage.", "Try again; if it fails again, include the stage and error code when reporting the problem."),
    "unsupported_source_file": ("The builder could not handle a file format included in the app folder.", "Use the latest app build with support for bundled binary model files."),
    "dependency_notice_required": ("A Python dependency is missing its required license text.", "Include the dependency's original license as LICENSE followed by a hyphen and its package name in the app folder."),
    "unsupported_mcp_sdk_version": ("The app runtime does not contain the supported MCP SDK.", "Use the latest app build, which includes the host's supported SDK for ordinary Python apps."),
    "agent_sdk_closure_invalid": ("The bundled MCP SDK could not be verified or copied into the app runtime.", "Use a complete signed app build and try preparation again."),
    "agent_sdk_dependency_conflict": ("The app's dependency versions conflict with the bundled MCP SDK.", "Update the conflicting application dependency before preparing the app."),
}


def safe_code(error, fallback="app_setup_failed"):
    code = getattr(error, "code", error if isinstance(error, str) else None)
    return code if code in DIAGNOSTICS else fallback


def diagnostic(error):
    code = safe_code(error)
    message, action = DIAGNOSTICS[code]
    return {"code": code, "message": message, "action": action}
