//! Offline integration replay using the same protocol, ADPCM and output code.
//! Input: host_time_ns<TAB>ctl|rx<TAB>hex. Output is 16kHz mono s16 raw PCM.
#[path = "../src/adpcm.rs"] mod adpcm;
#[path = "../src/protocol/mod.rs"] mod protocol;
#[path = "../src/remote_audio.rs"] mod remote_audio;
use protocol::Protocol;
use remote_audio::{PcmEvent, RemoteAudio};
use std::{fs, io::Write};

fn main() -> anyhow::Result<()> {
    let args: Vec<_> = std::env::args().collect();
    anyhow::ensure!(args.len() == 4, "trace.tsv output.pcm decoded.pcm");
    let trace = fs::read_to_string(&args[1])?;
    let events: Vec<_> = trace.lines().map(|line| {
        let fields: Vec<_> = line.split('\t').collect();
        let bytes = fields[2].as_bytes().chunks_exact(2).map(|hex|
            u8::from_str_radix(std::str::from_utf8(hex).unwrap(), 16).unwrap()).collect::<Vec<_>>();
        (fields[0].parse::<u64>().unwrap(), fields[1].to_owned(), bytes)
    }).collect();
    let caps = protocol::parse_caps_resp(&[0x0b, 1, 0, 2, 3, 0, 120]).unwrap();
    let mut decoder = protocol::v10::ProtocolV10::new();
    decoder.on_caps_resp(&caps)?;
    decoder.set_reset_on_start(true);
    let mut audio = RemoteAudio::new(16000, 220, 1.0);
    let mut output = fs::File::create(&args[2])?;
    let mut decoded = fs::File::create(&args[3])?;
    let mut offset = 0;
    let final_time = events.last().unwrap().0 + 1_000_000_000;
    let mut block = [0i16; 256];
    for now in (0..=final_time).step_by(16_000_000) {
        while offset < events.len() && events[offset].0 <= now {
            let (_, ref kind, ref payload) = events[offset];
            if kind == "ctl" {
                match decoder.parse_ctl(payload) {
                    protocol::types::CtlEvent::AudioStart { .. } => {
                        decoder.on_audio_start(); audio.push(PcmEvent::Begin);
                    }
                    protocol::types::CtlEvent::AudioStop { .. } => audio.push(PcmEvent::End),
                    protocol::types::CtlEvent::AudioSync(ref sync) => decoder.on_audio_sync(sync),
                    _ => {}
                }
            } else if kind == "rx" {
                let mut pcm = decoder.decode_audio(payload).expect("valid captured frame").samples;
                adpcm::declip(&mut pcm); adpcm::lowpass(&mut pcm);
                for sample in &pcm { decoded.write_all(&sample.to_le_bytes())?; }
                audio.push(PcmEvent::Samples(pcm));
            }
            offset += 1;
        }
        audio.render(&mut block);
        for sample in block { output.write_all(&sample.to_le_bytes())?; }
    }
    println!("{:?}", audio.stats());
    anyhow::ensure!(audio.stats().queued_samples == 0, "unplayed tail");
    anyhow::ensure!(audio.stats().dropped_samples == 0, "dropped captured samples");
    Ok(())
}
