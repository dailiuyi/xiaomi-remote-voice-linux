//! Compatibility mode bounds the decoder-to-audio-thread transport too.
use crate::remote_audio::PcmEvent;
use std::sync::mpsc;
pub enum Sender {
    Legacy(mpsc::Sender<PcmEvent>),
    Bounded(tokio::sync::mpsc::Sender<PcmEvent>),
}
pub enum Receiver {
    Legacy(mpsc::Receiver<PcmEvent>),
    Bounded(tokio::sync::mpsc::Receiver<PcmEvent>),
}
pub fn channel(bounded: bool) -> (Sender, Receiver) {
    if bounded {
        let (tx, rx) = tokio::sync::mpsc::channel(16);
        (Sender::Bounded(tx), Receiver::Bounded(rx))
    } else {
        let (tx, rx) = mpsc::channel();
        (Sender::Legacy(tx), Receiver::Legacy(rx))
    }
}
impl Sender {
    pub async fn send(&self, event: PcmEvent) -> Result<(), ()> {
        match self {
            Self::Legacy(tx) => tx.send(event).map_err(|_| ()),
            Self::Bounded(tx) => tx.send(event).await.map_err(|_| ()),
        }
    }
}
impl Receiver {
    pub fn try_recv(&mut self) -> Result<PcmEvent, mpsc::TryRecvError> {
        match self {
            Self::Legacy(rx) => rx.try_recv(),
            Self::Bounded(rx) => rx.try_recv().map_err(|error| match error {
                tokio::sync::mpsc::error::TryRecvError::Empty => mpsc::TryRecvError::Empty,
                tokio::sync::mpsc::error::TryRecvError::Disconnected => {
                    mpsc::TryRecvError::Disconnected
                }
            }),
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn bounded_transport_waits_without_reordering_boundary_events() {
        let (sender, mut receiver) = channel(true);
        sender.send(PcmEvent::Begin).await.unwrap();
        for _ in 0..15 {
            sender.send(PcmEvent::Samples(vec![1; 240])).await.unwrap();
        }
        let pending = sender.send(PcmEvent::End);
        tokio::pin!(pending);
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(1), &mut pending)
                .await
                .is_err()
        );
        assert!(matches!(receiver.try_recv(), Ok(PcmEvent::Begin)));
        pending.await.unwrap();
        for _ in 0..15 {
            assert!(matches!(receiver.try_recv(), Ok(PcmEvent::Samples(_))));
        }
        assert!(matches!(receiver.try_recv(), Ok(PcmEvent::End)));
    }
}
