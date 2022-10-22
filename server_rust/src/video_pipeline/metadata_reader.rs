use std::process::Command;
use std::sync::atomic::Ordering;
use threadpool::ThreadPool;
use std::path::{PathBuf};
use serde_json;
use crossbeam_channel::{Sender, Receiver, RecvError};
use tracing;
use rust_decimal::prelude::*;
use std::sync::atomic::AtomicBool;

use super::{IncomingFile, DetailedMsg};


#[derive(Debug, Clone)]
pub struct Metadata {
    pub src_file: PathBuf,
    pub user_id: String,
    pub total_frames: u32,
    pub duration: Decimal,
    pub orig_codec: String,
    pub fps: Decimal,
    pub bitrate: u32,
    pub metadata_all: String,
}

pub type MetadataResult = Result<Metadata, DetailedMsg>;

/// Run Mediainfo shell command and return the output
/// 
/// # Arguments
/// * `file_path` - Path to the file to be analyzed
fn run_mediainfo( file: &PathBuf ) -> Result<serde_json::Value, String>
{
    match Command::new("mediainfo").arg("--Output=JSON").arg(file).output()
    {
        Ok(output) => {
            if output.status.success() {                
                {
                    let json_res = String::from_utf8(output.stdout)
                        .map_err(|e| e.to_string())?;
                    serde_json::from_str(&json_res)
                }.map_err(|e| format!("Error parsing mediainfo JSON: {:?}", e))
            } else {
                Err( format!("Mediainfo exited with error: {}",
                    String::from_utf8_lossy(&output.stderr)))
            }
        },
        Err(e) => {
            Err(format!("Failed to execute mediainfo: {}", e))
        }
    }
}

/// Parse mediainfo JSON output and return the metadata object.
/// Possibly returned error message contains details to be sent to the client
/// in the DetailedMsg struct.
/// 
/// # Arguments
/// * `json` - Mediainfo JSON output
/// * `args` - Metadata request arguments
/// * `get_file_size` - Closure to get the file size (only called if bitrate is not available and we need to calculate it)
fn extract_variables<F>(json: serde_json::Value, args: &IncomingFile, get_file_size: F) -> Result<Metadata, String>
    where F: FnOnce() -> Result<u64, String>
{
    let tracks = json["media"]["track"].as_array().ok_or("No media tracks found")?;
    let video_track = tracks.iter().find(|t| t["@type"] == "Video").ok_or("No video track found")?;
    let fps = video_track["FrameRate"].as_str().ok_or("FPS not found")?;
    let frame_count = video_track["FrameCount"].as_str().ok_or("FrameCount not found")?;

    let duration_str = video_track["Duration"].as_str().ok_or("Duration not found")?;
    let duration = Decimal::from_str(duration_str).map_err(|_| format!("Invalid duration: {}", fps))?;

    // Bitrate is tricky. It might be in "BitRate" or "BitRate_Nominal". If it's not in either, we'll estimate it.
    let bitrate = {
        let bitrate_str = video_track["BitRate"].as_str()
            .or(video_track["BitRate_Nominal"].as_str());
        match bitrate_str {
            Some(bit_rate_str) => bit_rate_str.parse().map_err(|_| format!("Invalid bitrate: {}", bit_rate_str))?,
            None => {
                let duration = duration.to_f32().ok_or("Invalid duration")?;
                ((get_file_size()? as f32) * 8.0 / duration) as u32
            }}};

    Ok(Metadata {
        src_file: args.file_path.clone(),
        user_id: args.user_id.clone(),
        total_frames: frame_count.parse().map_err(|e| format!("Error parsing frame count: {}", e))?,
        duration: duration,
        orig_codec: video_track["Format"].as_str().ok_or("No codec found")?.to_string(),
        fps:  Decimal::from_str(fps).map_err(|_| format!("Invalid FPS: {}", fps))?,
        bitrate: bitrate,
        metadata_all: json.to_string()
    })
}

/// Run mediainfo and extract the metadata
fn read_metadata_from_file(args: &IncomingFile) -> Result<Metadata, String>
{
    let json = run_mediainfo(&args.file_path)?;
    extract_variables(json, args, || Ok(args.file_path.metadata().map_err(|e| format!("Failed to get file size: {:?}", e))?.len()))
}

/// Listens to inq for new videos to scan for metadata with Mediainfo shell command.
/// When a new file is received, it is processed and the result is sent to outq.
/// Starts a thread pool of `n_workers` workers to support simultaneous processing of multiple files.
/// Exits when inq is closed or outq stops accepting messages.
/// 
/// # Arguments
/// * `inq` - channel to receive new files to process
/// * `outq` - channel to send results to
/// * `n_workers` - number of threads to use for processing
pub fn run_forever(inq: Receiver<IncomingFile>, outq: Sender<MetadataResult>, n_workers: usize)
{
    tracing::info!("Starting.");
    let pool = ThreadPool::new(n_workers);
    let pool_is_healthy  = std::sync::Arc::new(AtomicBool::new(true));

    while pool_is_healthy.load(Ordering::Relaxed) {
        match inq.recv() {
            Ok(args) => {
                tracing::info!("Got message: {:?}", args);
                let pool_is_healthy = pool_is_healthy.clone();
                let outq = outq.clone();
                pool.execute(move || {
                    if let Err(e) = outq.send(
                        read_metadata_from_file(&args).map_err(|e| {
                                DetailedMsg {
                                    msg: "Metadata read failed".to_string(),
                                    details: e,
                                    src_file: args.file_path.clone(),
                                    user_id: args.user_id.clone() }}))
                    {
                        tracing::error!("Result send failed! Aborting. -- {:?}", e);
                        pool_is_healthy.store(false, Ordering::Relaxed);
                    }});
            },
            Err(RecvError) => {
                tracing::info!("Channel closed. Exiting.");
                break;
            }
        }
    }

    tracing::warn!("Clean exit.");
}


// Unit tests =====================================================================================

#[cfg(test)]
fn test_fixture(has_bitrate: bool, has_fps: bool) -> (IncomingFile, serde_json::Value)
{
    let bitrate = if has_bitrate { r#", "BitRate": "1000""# } else { "" };
    let fps = if has_fps { r#", "FrameRate": "30""# } else { "" };

    let json = serde_json::from_str(&format!(r#"{{
        "media": {{ "track": [ {{
                    "@type": "Video",  "FrameCount": "100",
                    "Duration": "5.0", "Format": "H264"
                    {}{}
                }} ] }} }}"#, bitrate, fps)).unwrap();

    let args = IncomingFile {
        file_path: PathBuf::from("test.mp4"),
        user_id: "test_user".to_string()};

    (args, json)
}

#[test]
fn test_extract_variables_ok() 
{
    let (args, json) = test_fixture(true, true);
    let metadata = extract_variables(json, &args, || Ok(1000)).unwrap();
    assert_eq!(metadata.total_frames, 100);
    assert_eq!(metadata.duration, Decimal::from_str("5").unwrap());
    assert_eq!(metadata.orig_codec, "H264");
    assert_eq!(metadata.fps, Decimal::from_str("30.000").unwrap());
    assert_eq!(metadata.bitrate, 1000);
}

#[test]
fn test_extract_variables_missing_bitrate() 
{
    let (args, json) = test_fixture(false, true);
    let metadata = extract_variables(json, &args, || Ok(1000)).unwrap();
    assert_eq!(metadata.bitrate, 1000*8/5);
}

#[test]
fn test_extract_variables_fail_missing_fps()
{
    let (args, json) = test_fixture(true, false);
    let metadata = extract_variables(json, &args, || Ok(1000));
    assert!(metadata.is_err());
    assert!(metadata.unwrap_err().to_lowercase().contains("fps"));
}
