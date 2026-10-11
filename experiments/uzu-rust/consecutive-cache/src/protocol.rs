//! Content-free worker entry point; diagnostics cannot enter the private protocol.
use std::{
    ffi::{OsStr, OsString},
    fs::{File, OpenOptions},
    io::{self, Write},
    os::fd::{AsRawFd, FromRawFd},
    path::PathBuf,
};

fn arguments(mut args: impl Iterator<Item = OsString>) -> Result<(PathBuf, bool), &'static str> {
    if args.next().as_deref() != Some(OsStr::new("--score-model-path")) {
        return Err("invalid_arguments");
    }
    let path = args.next().ok_or("invalid_arguments")?;
    let enabled = match args.next() {
        None => false,
        Some(flag) if flag == "--consecutive-cache" => true,
        _ => return Err("invalid_arguments"),
    };
    if args.next().is_some() {
        return Err("invalid_arguments");
    }
    Ok((PathBuf::from(path), enabled))
}

fn protocol_output() -> Result<File, &'static str> {
    let null = OpenOptions::new()
        .write(true)
        .open("/dev/null")
        .map_err(|_| "output_setup_failed")?;
    let fd = unsafe { libc::fcntl(libc::STDOUT_FILENO, libc::F_DUPFD_CLOEXEC, 3) };
    if fd < 0 {
        return Err("output_setup_failed");
    }
    let output = unsafe { File::from_raw_fd(fd) };
    if unsafe { libc::dup2(null.as_raw_fd(), libc::STDOUT_FILENO) } < 0
        || unsafe { libc::dup2(null.as_raw_fd(), libc::STDERR_FILENO) } < 0
    {
        return Err("output_setup_failed");
    }
    Ok(output)
}

pub fn main(digest_mode: bool) {
    std::panic::set_hook(Box::new(|_| {}));
    let Ok(output) = protocol_output() else {
        std::process::exit(1)
    };
    // Native captures are forbidden before any worker/model threads exist.
    unsafe {
        std::env::remove_var("UZU_CAPTURE_FIRST_PREFILL");
        std::env::remove_var("UZU_CAPTURE_FIRST_DECODE");
    }
    let mut output = io::BufWriter::new(output);
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let (path, enabled) = arguments(std::env::args_os().skip(1))?;
        crate::scoring::run_lines(&path, enabled, digest_mode, &mut output)
    }));
    let code = match result {
        Ok(Ok(())) => return,
        Ok(Err(code)) => code,
        Err(_) => "native_operation_failed",
    };
    let _ = serde_json::to_writer(
        &mut output,
        &serde_json::json!({"ready":false,"error":code}),
    );
    let _ = output.write_all(b"\n");
    let _ = output.flush();
    std::process::exit(1);
}

#[cfg(test)]
mod tests {
    use super::*;
    fn parse(args: &[&str]) -> Result<(PathBuf, bool), &'static str> {
        arguments(args.iter().map(OsString::from))
    }
    #[test]
    fn cache_requires_explicit_flag_and_rejects_unknown_or_duplicate_flags() {
        assert_eq!(
            parse(&["--score-model-path", "/model"]),
            Ok((PathBuf::from("/model"), false))
        );
        assert_eq!(
            parse(&["--score-model-path", "/model", "--consecutive-cache"]),
            Ok((PathBuf::from("/model"), true))
        );
        for args in [
            vec![],
            vec!["--score-model-path"],
            vec!["--other", "/model"],
            vec!["--score-model-path", "/model", "--unknown"],
            vec![
                "--score-model-path",
                "/model",
                "--consecutive-cache",
                "--consecutive-cache",
            ],
        ] {
            assert_eq!(parse(&args), Err("invalid_arguments"));
        }
    }
}
