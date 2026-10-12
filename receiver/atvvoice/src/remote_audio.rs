//! Bounded PCM output conditioning for the RC003-MS receiver.
#[derive(Debug)]
pub enum PcmEvent {
    Begin,
    Samples(Vec<i16>),
    End,
}
#[derive(Clone, Copy, Debug, Default)]
pub struct AudioStats {
    pub underruns: u64,
    pub missing_samples: u64,
    pub dropped_samples: u64,
    pub queued_samples: usize,
    /// Minimum total queued PCM during normal ongoing playback; zero until
    /// any such playback occurs. End fades and recovery fades are excluded.
    pub min_playback_queue_samples: usize,
    pub played_samples: u64,
    pub interrupted_sessions: u64,
}
use std::collections::VecDeque;

struct Segment {
    samples: VecDeque<f32>,
    ended: bool,
    playing: bool,
    recovering: bool,
    ramp_position: usize,
    fade_remaining: usize,
    fade_length: usize,
}
impl Segment {
    fn new() -> Self {
        Self {
            samples: VecDeque::new(),
            ended: false,
            playing: false,
            recovering: false,
            ramp_position: 0,
            fade_remaining: 0,
            fade_length: 0,
        }
    }
}

/// PCM events must retain receiver order. `render` is called by the audio thread.
/// The total queue is capped at 500 ms: overflow rejects newest samples and counts
/// them rather than silently overwriting an already accepted session tail.
/// End flushes even sessions shorter than the configured initial prebuffer.
/// Queueing is bounded, but processing a Samples event is proportional to its size.
pub struct RemoteAudio {
    segments: VecDeque<Segment>,
    prebuffer: usize,
    capacity: usize,
    fade_in: usize,
    tail: usize,
    gain: f32,
    hp_alpha: f32,
    hp_x: f32,
    hp_y: f32,
    stats: AudioStats,
    have_queue_min: bool,
}
impl RemoteAudio {
    pub fn new(sample_rate: u32, prebuffer_ms: u32, gain_linear: f32) -> Self {
        assert!(sample_rate > 0, "sample rate must be positive");
        let samples = |ms: u32| ((sample_rate as u64 * ms as u64 / 1000) as usize).max(1);
        let capacity = samples(500);
        let tail = samples(16).min(capacity.saturating_sub(1));
        let prebuffer = samples(prebuffer_ms.min(500)).max(tail + 1).min(capacity);
        let dt = 1.0 / sample_rate as f32;
        let rc = 1.0 / (std::f32::consts::TAU * 80.0);
        Self {
            segments: VecDeque::new(),
            prebuffer,
            capacity,
            fade_in: samples(32),
            tail,
            gain: if gain_linear.is_finite() {
                gain_linear.max(0.0)
            } else {
                1.0
            },
            hp_alpha: rc / (rc + dt),
            hp_x: 0.0,
            hp_y: 0.0,
            stats: AudioStats::default(),
            have_queue_min: false,
        }
    }

    pub fn push(&mut self, event: PcmEvent) {
        match event {
            PcmEvent::Begin => {
                // Keep accepted audio from the preceding segment in output order.
                // A missing End is repaired explicitly and counted, never hidden.
                if let Some(previous) = self.segments.back_mut() {
                    if !previous.ended {
                        previous.ended = true;
                        self.stats.interrupted_sessions += 1;
                    }
                }
                // Empty completed segments carry no tail and must not grow
                // memory when control events arrive without PCM.
                if self.segments.back().is_some_and(|s| s.samples.is_empty()) {
                    self.segments.pop_back();
                }
                self.hp_x = 0.0;
                self.hp_y = 0.0;
                self.segments.push_back(Segment::new());
            }
            PcmEvent::Samples(samples) => {
                let accepting = self.segments.back().is_some_and(|s| !s.ended);
                if !accepting {
                    self.stats.dropped_samples += samples.len() as u64;
                    return;
                }
                let segment = self.segments.back_mut().unwrap();
                for sample in samples {
                    let x = sample as f32 / 32768.0;
                    // Run before gain, queueing, or any synthetic zero insertion.
                    let y = self.hp_alpha * (self.hp_y + x - self.hp_x);
                    self.hp_x = x;
                    self.hp_y = y;
                    if self.stats.queued_samples < self.capacity {
                        segment.samples.push_back(y);
                        self.stats.queued_samples += 1;
                    } else {
                        self.stats.dropped_samples += 1;
                    }
                }
            }
            PcmEvent::End => {
                if let Some(segment) = self.segments.back_mut() {
                    segment.ended = true;
                }
            }
        }
    }

    pub fn render(&mut self, out: &mut [i16]) {
        out.fill(0);
        for target in out {
            // Completed segments are removed without consuming a device sample.
            while self
                .segments
                .front()
                .is_some_and(|s| s.ended && s.samples.is_empty())
            {
                self.segments.pop_front();
            }
            let Some(segment) = self.segments.front_mut() else {
                continue;
            };
            if !segment.playing {
                if segment.samples.len() >= self.prebuffer
                    || (segment.ended && !segment.samples.is_empty())
                {
                    segment.playing = true;
                    segment.ramp_position = 0;
                    segment.recovering = false;
                } else {
                    // The initial prebuffer is intentional latency. A later
                    // rebuffer gap represents genuinely unavailable PCM.
                    if segment.recovering {
                        self.stats.missing_samples += 1;
                    }
                    continue;
                }
            }
            if segment.fade_remaining == 0 {
                if segment.ended && segment.samples.len() <= self.tail {
                    segment.fade_length = segment.samples.len();
                    segment.fade_remaining = segment.fade_length;
                } else if !segment.ended && segment.samples.len() <= self.tail {
                    // Spend the held lookahead as a smooth exit before zeros.
                    // Do not restart until a bounded prebuffer is available.
                    self.stats.underruns += 1;
                    segment.fade_length = segment.samples.len();
                    segment.fade_remaining = segment.fade_length;
                    segment.recovering = true;
                }
            }
            let Some(sample) = segment.samples.pop_front() else {
                segment.playing = false;
                if segment.recovering {
                    self.stats.missing_samples += 1;
                }
                continue;
            };
            let available = segment.samples.len() + 1;
            if !segment.recovering && !segment.ended {
                if !self.have_queue_min || available < self.stats.min_playback_queue_samples {
                    self.stats.min_playback_queue_samples = available;
                    self.have_queue_min = true;
                }
            }
            self.stats.queued_samples -= 1;
            self.stats.played_samples += 1;
            let start_gain = sine_ramp(segment.ramp_position, self.fade_in);
            segment.ramp_position = segment.ramp_position.saturating_add(1);
            let end_gain = if segment.fade_remaining > 0 {
                let gain = sine_ramp(segment.fade_remaining - 1, segment.fade_length);
                segment.fade_remaining -= 1;
                gain
            } else {
                1.0
            };
            let conditioned = soft_limit(sample * self.gain) * start_gain * end_gain;
            *target = (conditioned * 32768.0).round() as i16;
            if segment.fade_remaining == 0 && segment.recovering {
                segment.playing = false;
            }
        }
    }
    pub fn stats(&self) -> AudioStats {
        self.stats
    }
}

fn sine_ramp(position: usize, length: usize) -> f32 {
    if length <= 1 {
        return 0.0;
    }
    let progress = position.min(length - 1) as f32 / (length - 1) as f32;
    (progress * std::f32::consts::FRAC_PI_2).sin().powi(2)
}
fn soft_limit(sample: f32) -> f32 {
    let magnitude = sample.abs();
    if magnitude <= 0.65 {
        return sample;
    }
    // Smooth derivative at the knee, with a strict asymptote at 0.82.
    sample.signum() * (0.65 + 0.17 * (1.0 - (-(magnitude - 0.65) / 0.17).exp()))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn wave(n: usize) -> Vec<i16> {
        (0..n)
            .map(|i| (12000.0 * (i as f32 * std::f32::consts::TAU * 1000.0 / 16000.0).sin()) as i16)
            .collect()
    }
    fn ended(n: usize) -> RemoteAudio {
        let mut a = RemoteAudio::new(16000, 220, 1.0);
        a.push(PcmEvent::Begin);
        a.push(PcmEvent::Samples(wave(n)));
        a.push(PcmEvent::End);
        a
    }
    #[test]
    fn idle_and_initial_prebuffer_are_not_underruns() {
        let mut a = RemoteAudio::new(16000, 220, 1.0);
        let mut out = [9; 512];
        a.render(&mut out);
        assert_eq!(out, [0; 512]);
        a.push(PcmEvent::Begin);
        a.push(PcmEvent::Samples(wave(240)));
        a.render(&mut out);
        assert_eq!(out, [0; 512]);
        assert_eq!(a.stats().underruns, 0);
        assert_eq!(a.stats().missing_samples, 0);
        assert_eq!(a.stats().queued_samples, 240);
    }
    #[test]
    fn short_ended_session_drains_without_waiting_for_prebuffer() {
        let mut a = ended(1000);
        let mut out = vec![0; 1500];
        a.render(&mut out);
        assert!(out.iter().any(|x| *x != 0));
        assert_eq!(a.stats().played_samples, 1000);
        assert_eq!(a.stats().queued_samples, 0);
        assert_eq!(a.stats().underruns, 0);
        assert_eq!(out[999], 0);
        assert_eq!(&out[1000..], &[0; 500]);
    }
    #[test]
    fn ended_tail_is_not_discarded_on_next_begin() {
        let mut a = ended(1200);
        a.push(PcmEvent::Begin);
        a.push(PcmEvent::Samples(wave(900)));
        a.push(PcmEvent::End);
        let mut out = vec![0; 2500];
        a.render(&mut out);
        assert_eq!(a.stats().played_samples, 2100);
        assert_eq!(a.stats().dropped_samples, 0);
        assert!(out[1200..2100].iter().any(|x| *x != 0));
    }
    #[test]
    fn begin_resets_highpass_state() {
        let mut a = ended(1000);
        let mut discard = vec![0; 1000];
        a.render(&mut discard);
        a.push(PcmEvent::Begin);
        a.push(PcmEvent::Samples(wave(1800)));
        a.push(PcmEvent::End);
        let mut got = vec![0; 1800];
        a.render(&mut got);
        let mut fresh = ended(1800);
        let mut expected = vec![0; 1800];
        fresh.render(&mut expected);
        assert!(expected.iter().any(|x| *x != 0));
        assert_eq!(got, expected);
    }
    #[test]
    fn highpass_removes_sustained_dc_before_queueing() {
        let mut a = RemoteAudio::new(16000, 0, 1.0);
        a.push(PcmEvent::Begin);
        a.push(PcmEvent::Samples(vec![24000; 5000]));
        a.push(PcmEvent::End);
        let mut out = vec![0; 5000];
        a.render(&mut out);
        assert!(out[..512].iter().any(|x| *x != 0));
        assert!(out[2000..4500].iter().all(|x| x.abs() <= 1));
    }
    #[test]
    fn highpass_is_continuous_across_packet_boundaries() {
        let mut a = ended(6000);
        let mut expected = vec![0; 6000];
        a.render(&mut expected);
        let mut b = RemoteAudio::new(16000, 220, 1.0);
        b.push(PcmEvent::Begin);
        for chunk in wave(6000).chunks(240) {
            b.push(PcmEvent::Samples(chunk.to_vec()));
        }
        b.push(PcmEvent::End);
        let mut got = vec![0; 6000];
        b.render(&mut got);
        assert!(expected.iter().any(|x| *x != 0));
        assert_eq!(got, expected);
    }
    #[test]
    fn queue_is_bounded_and_overflow_is_counted() {
        let mut a = RemoteAudio::new(16000, 220, 1.0);
        a.push(PcmEvent::Begin);
        a.push(PcmEvent::Samples(wave(11000)));
        assert_eq!(a.stats().queued_samples, 8000);
        assert_eq!(a.stats().dropped_samples, 3000);
    }
    #[test]
    fn output_is_soft_limited_and_onset_and_end_are_silent() {
        let mut a = RemoteAudio::new(16000, 220, 30.0);
        a.push(PcmEvent::Begin);
        a.push(PcmEvent::Samples(wave(5000)));
        a.push(PcmEvent::End);
        let mut out = vec![0; 5000];
        a.render(&mut out);
        assert!(out.iter().all(|x| (*x as i32).abs() <= 26870));
        assert!(out.iter().any(|x| (*x as i32).abs() > 20000));
        assert_eq!(out[0], 0);
        assert_eq!(out[4999], 0);
        assert!(out[1].abs() < 50);
        assert!(out[4998].abs() < 50);
    }
    #[test]
    fn starvation_fades_then_rebuffers_and_counts_real_missing_samples() {
        let mut a = RemoteAudio::new(16000, 100, 1.0);
        a.push(PcmEvent::Begin);
        a.push(PcmEvent::Samples(wave(1800)));
        let mut out = vec![0; 2300];
        a.render(&mut out);
        assert_eq!(a.stats().underruns, 1);
        assert_eq!(a.stats().missing_samples, 500);
        assert_eq!(out[1799], 0);
        assert_eq!(&out[1800..], &[0; 500]);
        a.push(PcmEvent::Samples(wave(240)));
        let mut wait = [1; 200];
        a.render(&mut wait);
        assert_eq!(wait, [0; 200]);
        assert_eq!(a.stats().underruns, 1);
        a.push(PcmEvent::Samples(wave(1800)));
        let mut recovered = [0; 600];
        a.render(&mut recovered);
        assert!(recovered.iter().any(|x| *x != 0));
        assert_eq!(recovered[0], 0);
        assert_eq!(a.stats().underruns, 1);
    }
    #[test]
    fn end_during_rebuffer_flushes_all_received_samples() {
        let mut a = RemoteAudio::new(16000, 100, 1.0);
        a.push(PcmEvent::Begin);
        a.push(PcmEvent::Samples(wave(1800)));
        a.render(&mut vec![0; 2000]);
        a.push(PcmEvent::Samples(wave(240)));
        a.push(PcmEvent::End);
        a.render(&mut vec![0; 500]);
        assert_eq!(a.stats().played_samples, 2040);
        assert_eq!(a.stats().queued_samples, 0);
    }
    #[test]
    fn unexpected_begin_closes_previous_session_with_accounting() {
        let mut a = RemoteAudio::new(16000, 220, 1.0);
        a.push(PcmEvent::Begin);
        a.push(PcmEvent::Samples(wave(1000)));
        a.push(PcmEvent::Begin);
        a.push(PcmEvent::Samples(wave(1000)));
        a.push(PcmEvent::End);
        a.render(&mut vec![0; 2100]);
        assert_eq!(a.stats().played_samples, 2000);
        assert_eq!(a.stats().interrupted_sessions, 1);
    }
    #[test]
    fn rendering_in_small_callbacks_preserves_pcm_and_session_tail() {
        let mut a = ended(5000);
        let mut expected = vec![0; 5200];
        a.render(&mut expected);
        let mut b = ended(5000);
        let mut got = vec![0; 5200];
        for output in got.chunks_mut(113) {
            b.render(output);
        }
        assert_eq!(got, expected);
        assert_eq!(b.stats().played_samples, 5000);
        assert_eq!(b.stats().missing_samples, 0);
    }
    #[test]
    fn end_releases_reserved_tail_without_spurious_underrun() {
        let mut a = RemoteAudio::new(16000, 220, 1.0);
        a.push(PcmEvent::Begin);
        a.push(PcmEvent::Samples(wave(4000)));
        a.render(&mut vec![0; 3744]);
        assert_eq!(a.stats().queued_samples, 256);
        assert_eq!(a.stats().underruns, 0);
        a.push(PcmEvent::End);
        let mut tail = [0; 256];
        a.render(&mut tail);
        assert!(tail.iter().any(|x| *x != 0));
        assert_eq!(tail[255], 0);
        assert_eq!(a.stats().played_samples, 4000);
        assert_eq!(a.stats().underruns, 0);
    }
    #[test]
    fn malformed_empty_control_bursts_remain_bounded() {
        let mut a = RemoteAudio::new(16000, 220, 1.0);
        for _ in 0..10000 {
            a.push(PcmEvent::Begin);
            a.push(PcmEvent::End);
        }
        assert_eq!(a.segments.len(), 1);
        assert_eq!(a.stats().queued_samples, 0);
        a.render(&mut [0; 128]);
        assert!(a.segments.is_empty());
    }
    #[test]
    fn orphan_pcm_is_rejected_and_counted() {
        let mut a = ended(1000);
        a.push(PcmEvent::Samples(wave(240)));
        assert_eq!(a.stats().dropped_samples, 240);
        assert_eq!(a.stats().queued_samples, 1000);
        a.render(&mut vec![0; 1200]);
        assert_eq!(a.stats().played_samples, 1000);
    }
}
