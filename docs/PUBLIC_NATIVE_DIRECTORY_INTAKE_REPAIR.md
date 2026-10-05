# Public native directory acceptance repair

The public Python acceptance entry uses the ordinary native folder-selection
handler. Automatic intake deliberately retains the existing `local_folder`
snapshot for directories containing only supported text files. Mixed directories
use `application_directory`. The acceptance entry previously required only the
latter and rejected valid callable and dynamic MCP fixtures after import.

The public Python entry now permits exactly those two directory kinds. The
command entry still requires `application_directory`. The imported draft must
remain editable, unattached, and bound to the exact selected source path; the
private fixture, manager, nonce, directory identities, and owner request guards
are unchanged. This changes developer acceptance behavior, with no backend,
recipe, release-policy, or execution-authority changes.

When native acceptance fails, the public Python driver retains only fixed
allowlisted stage/error codes, exit-success, and boolean selection/result flags
in its failure report before joined cleanup removes private state. Unknown codes
become `native_error_unclassified`; unknown stages become `unclassified`. Source,
request/response bodies, route paths, IDs, raw output, and exception text are not
retained by this diagnostic projection.

Controls compile production AppKit in a private scratch directory and exercise
the real owner Unix-socket router and actual automatic source snapshots. They
check text-only callable/MCP and mixed intake, reject prepared/unsupported source
kinds in adversarial owner envelopes, and preserve command text-only rejection.
The source-only host keeps execution closed; native selection success is proved
separately from preparation, with zero preparation/resolution jobs and no start
requests. These controls do not replace the final signed public family pipeline.

The original signed v3 failure and single causal reproduction are preserved.
Neither existing signed app nor its embedded source was modified. The corrected
signed app must be rebuilt and independently accepted before claiming that the
public callable/MCP/ONNX flow passes.
