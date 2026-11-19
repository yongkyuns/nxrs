async video => {
    const [track] = video.srcObject.getAudioTracks();
    if (!track || track.readyState !== 'live') throw new Error('no incoming microphone');
    const peer = window.nxrsPeers.find(p => p.connectionState === 'connected');
    const stats = await peer.getStats();
    const stream = Array.from(stats.values()).find(s => s.type === 'inbound-rtp' && s.kind === 'audio');
    if (stats.get(stream?.codecId)?.mimeType.toLowerCase() !== 'audio/opus' || !stream.packetsReceived)
        throw new Error('no received Opus packets');
    const lines = peer.remoteDescription.sdp.split(/\r?\n/);
    const names = new Set(lines.filter(l => /^a=ssrc:\d+ cname:/.test(l)).map(l => l.split(' cname:')[1]));
    const ids = lines.filter(l => l.startsWith('a=msid:')).map(l => l.slice(7).split(' '));
    if (names.size !== 1 || ids.length !== 2 || ids[0][0] !== ids[1][0] || ids[0][1] === ids[1][1])
        throw new Error('tracks do not share a synchronization context');
    const context = new AudioContext();
    const audio = context.createMediaStreamSource(new MediaStream([track]));
    const analyser = context.createAnalyser();
    analyser.fftSize = 1024;
    audio.connect(analyser);
    const samples = new Float32Array(analyser.fftSize);
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 1;
    const image = canvas.getContext('2d', {willReadFrequently: true});
    const light = [], tone = [], frequencies = [];
    let wasBright, wasLoud, callback;
    function frame(now, metadata) {
        image.drawImage(video, 0, 0, 1, 1);
        const bright = image.getImageData(0, 0, 1, 1).data[0] > 160;
        if (bright && wasBright === false) light.push(metadata.expectedDisplayTime);
        wasBright = bright;
        callback = video.requestVideoFrameCallback(frame);
    }
    callback = video.requestVideoFrameCallback(frame);
    try {
        await Promise.race([context.resume(), new Promise((_, reject) =>
            setTimeout(() => reject(new Error('Web Audio did not start')), 5000))]);
        const until = performance.now() + 12000;
        while (light.length < 6 || tone.length < 6) {
            if (performance.now() > until) throw new Error('light or tone absent');
            analyser.getFloatTimeDomainData(samples);
            let energy = 0, crossings = 0;
            for (let i = 0; i < samples.length; i++) {
                energy += samples[i] * samples[i];
                if (i && samples[i-1] <= 0 && samples[i] > 0) crossings++;
            }
            const loud = energy / samples.length > 0.015;
            if (loud && wasLoud === false) tone.push(performance.now());
            if (loud) frequencies.push(crossings * context.sampleRate / samples.length);
            wasLoud = loud;
            await new Promise(resolve => setTimeout(resolve, 12));
        }
        const frequency = frequencies.sort((a,b) => a-b)[Math.floor(frequencies.length/2)];
        if (frequency < 950 || frequency > 1050) throw new Error('unexpected tone');
        const offsets = light.slice(1,-1).map(v => Math.min(...tone.map(a => Math.abs(a-v))));
        return {codec: 'audio/opus', shared_timing_context: true, frequency_hz: Math.round(frequency), light_pulses: light.length, tone_pulses: tone.length,
                maximum_observed_cue_difference_ms: Math.round(Math.max(...offsets))};
    } finally {
        video.cancelVideoFrameCallback(callback);
        audio.disconnect();
        await context.close();
    }
}
