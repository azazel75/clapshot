use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, RwLock};
use std::sync::atomic::{AtomicBool};

use tokio::sync::Mutex;
use anyhow::anyhow;

use super::{WsMsgSender, SenderList, SenderListMap, StringToStringMap, Res};
use crate::database::DB;

/// Lists of all active connections and other server state vars
#[derive (Clone)]
pub struct ServerState {
    pub terminate_flag: Arc<AtomicBool>,
    pub db: Arc<DB>,
    pub videos_dir: PathBuf,
    pub upload_dir: PathBuf,
    pub url_base: String,
    user_id_to_senders: SenderListMap,
    video_hash_to_senders: SenderListMap,
    collab_id_to_senders: SenderListMap,
    collab_id_to_video_hash: StringToStringMap,
}

impl ServerState {

    pub fn new(db: Arc<DB>, videos_dir: &Path, upload_dir: &Path, url_base: &str, terminate_flag: Arc<AtomicBool>) -> ServerState {
        ServerState {
            db,
            videos_dir: videos_dir.to_path_buf(),
            upload_dir: upload_dir.to_path_buf(),
            terminate_flag,
            url_base: url_base.to_string(),
            user_id_to_senders: Arc::new(RwLock::new(HashMap::<String, SenderList>::new())),
            video_hash_to_senders: Arc::new(RwLock::new(HashMap::<String, SenderList>::new())),
            collab_id_to_senders: Arc::new(RwLock::new(HashMap::<String, SenderList>::new())),
            collab_id_to_video_hash: Arc::new(RwLock::new(HashMap::<String, String>::new())),
        }
    }

    /// Register a new sender (API connection) for a user_id. One user can have multiple connections.
    /// Returns a guard that will remove the sender when dropped.
    pub fn register_user_session(&self, user_id: &str, sender: WsMsgSender) -> Box<Mutex<dyn Send>> {
        self.add_sender_to_maplist(user_id, sender, &self.user_id_to_senders)
    }

    /// Send a message to all sessions user_id has open.
    /// Bails out with error if any of the senders fail.
    /// Returns the number of messages sent.
    pub fn send_to_all_user_sessions(&self, user_id: &str, msg: &super::Message) -> Res<u32> {
        let mut total_sent = 0u32;
        let map = self.user_id_to_senders.read().map_err(|e| anyhow!("Sender map poisoned: {}", e))?;
        for sender in map.get(user_id).unwrap_or(&vec![]).iter() {
            sender.send(msg.clone())?;
            total_sent += 1; };
        Ok(total_sent)
    }

    /// Send a message to all sessions that are collaboratively viewing a video.
    /// Bails out with error if any of the senders fail.
    /// Returns the number of messages sent.
    pub fn send_to_all_collab_users(&self, collab_id: &Option<String>, msg: &super::Message) -> Res<u32> {
        let mut total_sent = 0u32;
        if let Some(collab_id) = collab_id {
            let map = self.collab_id_to_senders.read().map_err(|e| anyhow!("Sender map poisoned: {}", e))?;
            for sender in map.get(collab_id).unwrap_or(&vec![]).iter() {
                sender.send(msg.clone())?;
                total_sent += 1; };
        }
        Ok(total_sent)
    }

    /// Register a new sender (API connection) as a viewer for a video.
    /// One video can have multiple viewers (including the same user, using different connections).
    /// Returns a guard that will remove the sender when dropped.
    pub fn link_session_to_video(&self, video_hash: &str, sender: WsMsgSender) -> Box<Mutex<dyn Send>> {
        self.add_sender_to_maplist(video_hash, sender, &self.video_hash_to_senders)
    }

    /// Remove video hash mappings from all collabs that have no more viewers.
    fn garbage_collect_collab_video_map(&self) {
        let mut map = self.collab_id_to_video_hash.write().unwrap();
        let senders = self.collab_id_to_senders.read().unwrap();
        map.retain(|collab_id, _| !senders.get(collab_id).unwrap_or(&vec![]).is_empty());
    }

    pub fn sender_is_collab_participant(&self, collab_id: &str, sender: &WsMsgSender) -> bool {
        let senders = self.collab_id_to_senders.read().unwrap();
        senders.get(collab_id).unwrap_or(&vec![]).iter().any(|s| s.same_channel(sender))
    }

    pub fn link_session_to_collab(&self, collab_id: &str, video_hash: &str, sender: WsMsgSender) -> Res<Box<Mutex<dyn Send>>> {
        // GC collab video map. (This might not be the optimal way to do this but at least it
        // will keep it from growing indefinitely.)
        self.garbage_collect_collab_video_map();

        // Only the first joiner (creator) of a collab gets to set the video hash.
        let mut map = self.collab_id_to_video_hash.write().unwrap();
        if !map.contains_key(collab_id) {
            map.insert(collab_id.to_string(), video_hash.to_string());
        } else if map.get(collab_id).unwrap() != video_hash {
            return Err(anyhow!("Mismatching video hash for pre-existing collab"));
        }        
        Ok(self.add_sender_to_maplist(collab_id, sender, &self.collab_id_to_senders))
    }

    /// Send a message to all sessions that are viewing a video.
    /// Bails out with error if any of the senders fail.
    /// Returns the number of messages sent.
    pub fn send_to_all_video_sessions(&self, video_hash: &str, msg: &super::Message) -> Res<u32> {
        let mut total_sent = 0u32;
        let map = self.video_hash_to_senders.read().map_err(|e| anyhow!("Sender map poisoned: {}", e))?;
        for sender in map.get(video_hash).unwrap_or(&vec![]).iter() {
            sender.send(msg.clone())?;
            total_sent += 1; };
        Ok(total_sent)
    }

    // Common implementations for the above add functions.
    fn add_sender_to_maplist(&self, key: &str, sender: WsMsgSender, maplist: &SenderListMap) -> Box<Mutex<dyn Send>> {
        let mut list = maplist.write().unwrap();
        let senders = list.entry(key.to_string()).or_insert(Vec::new());
        senders.push(sender.clone());

        struct Guard { maplist: SenderListMap, sender: WsMsgSender, key: String }
        impl Drop for Guard {
            fn drop(&mut self) {
                if let Ok(mut list) = self.maplist.write() {
                    let senders = list.entry(self.key.to_string()).or_insert(Vec::new());
                    senders.retain(|s| !self.sender.same_channel(&s));
                    if senders.len() == 0 { list.remove(&self.key); }
                } else { tracing::error!("SenderListMap was poisoned! Leaving a dangling API session."); }
            }}
        Box::new(Mutex::new(Guard { maplist: maplist.clone(), sender: sender.clone(), key: key.to_string() }))
    }
}
