/* Camera and call state live independently of the channel player. */
const videoCallPanel = document.createElement('section');
videoCallPanel.id = 'video-call-panel';
videoCallPanel.setAttribute('aria-label', 'Video call');
videoCallPanel.hidden = true;
videoCallPanel.innerHTML = `
  <div class="video-call-picture" hidden>
    <video class="call-remote" autoplay playsinline aria-label="Guest video"></video>
    <video class="call-local" autoplay muted playsinline aria-label="Your camera preview"></video>
  </div>
  <p class="call-status" role="status"></p>
  <input class="call-link" aria-label="Video call invite link" readonly hidden>
  <div class="call-actions">
    <button type="button" class="secondary" data-call-control="accept" hidden>Accept video call</button>
    <button type="button" class="secondary" data-call-control="copy" hidden>Copy invite link</button>
    <button type="button" class="secondary" data-call-control="audio" hidden>Mute microphone</button>
    <button type="button" class="secondary" data-call-control="video" hidden>Turn camera off</button>
    <button type="button" class="secondary" data-call-control="sound" hidden>Play call audio</button>
    <button type="button" class="secondary" data-call-control="end">End call</button>
  </div>`;

let videoCall = null;
let videoCallInvite = null;
const callElement = selector => videoCallPanel.querySelector(selector);
const callButton = action => callElement(`[data-call-control="${action}"]`);

function captureCallInvite() {
  const fragment = new URLSearchParams(location.hash.slice(1));
  if (!fragment.has('call')) return;
  const invite = fragment.get('call');
  fragment.delete('call');
  const rest = fragment.toString();
  history.replaceState(null, '', location.pathname + location.search + (rest ? '#' + rest : ''));
  if (!videoCall && /^[\w-]{32}$/.test(invite)) videoCallInvite = invite;
}
captureCallInvite();
window.addEventListener('hashchange', () => {
  captureCallInvite();
  if (state.user) showCallInvite();
});

function mountCallPanel() {
  if (videoCallPanel.hidden) return;
  const target = document.querySelector('.modal') || document.body;
  if (videoCallPanel.parentElement !== target) target.append(videoCallPanel);
}

function callStatus(message) {
  callElement('.call-status').textContent = message;
}

function showCallInvite() {
  if (!videoCallInvite || videoCall) return;
  videoCallPanel.hidden = false;
  mountCallPanel();
  callStatus('You have a video call invitation.');
  callButton('accept').hidden = false;
  callButton('end').textContent = 'Dismiss invitation';
}

async function startVideoCall(join = false) {
  if (videoCall) {
    callButton('end').focus();
    return;
  }
  if (!join && videoCallInvite) {
    showCallInvite();
    callButton('accept').focus();
    return;
  }
  if (!navigator.mediaDevices?.getUserMedia || !window.RTCPeerConnection) {
    toast('Video calls need a supported browser on HTTPS.');
    return;
  }

  const call = {join, invite: join ? videoCallInvite : null};
  videoCallInvite = null;
  videoCall = call;
  videoCallPanel.hidden = false;
  mountCallPanel();
  callButton('accept').hidden = true;
  callButton('end').textContent = 'End call';
  callButton('end').focus();
  callStatus('Allow access to your camera and microphone.');
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {echoCancellation: true, noiseSuppression: true},
      video: {width: {ideal: 640}, height: {ideal: 360}, frameRate: {ideal: 24}}
    }).catch(() => { throw new Error('Allow camera and microphone access and check that both are connected.'); });
    if (videoCall !== call) {
      stream.getTracks().forEach(track => track.stop());
      return;
    }
    call.stream = stream;
    callElement('.call-local').srcObject = stream;
    callElement('.video-call-picture').hidden = false;
    callButton('audio').hidden = false;
    callButton('video').hidden = false;

    const result = await api('video-call/' + (join ? 'join' : 'create'), {invite: call.invite});
    call.invite = result.invite || call.invite;
    if (videoCall !== call) {
      api('video-call/end', {invite: call.invite}).catch(() => {});
      return;
    }
    call.registered = true;
    const connection = new RTCPeerConnection({iceServers: result.ice_servers});
    call.connection = connection;
    connection.addEventListener('track', event => {
      if (videoCall !== call) return;
      const remote = callElement('.call-remote');
      remote.srcObject = event.streams[0];
      remote.play().catch(() => { if (videoCall === call) callButton('sound').hidden = false; });
    });
    connection.addEventListener('connectionstatechange', () => {
      if (videoCall !== call) return;
      if (connection.connectionState === 'connected') {
        call.connected = true;
        call.disconnectedAt = null;
        callStatus('Video call connected.');
      } else if (connection.connectionState === 'failed') {
        finishVideoCall('The video call could not connect.');
      } else if (connection.connectionState === 'disconnected') {
        call.disconnectedAt ||= Date.now();
        callStatus('Reconnecting video call…');
      }
    });
    for (const track of stream.getTracks()) connection.addTrack(track, stream);

    if (!join) {
      const url = new URL('/', location.origin);
      url.hash = new URLSearchParams({call: call.invite});
      const link = callElement('.call-link');
      link.value = url.href;
      link.hidden = false;
      callButton('copy').hidden = false;
      callStatus('Share the invite link. Waiting for your guest…');
    } else {
      callStatus('Connecting video call…');
    }
    pollVideoCall(call);
  } catch (error) {
    if (videoCall === call) finishVideoCall(error.message);
  }
}

async function publishCallDescription(call) {
  const connection = call.connection;
  const description = await (call.join ? connection.createAnswer() : connection.createOffer());
  await connection.setLocalDescription(description);
  if (connection.iceGatheringState !== 'complete') {
    await new Promise(resolve => {
      const check = () => {
        if (connection.iceGatheringState === 'complete') finish();
      };
      const finish = () => {
        clearTimeout(timer);
        connection.removeEventListener('icegatheringstatechange', check);
        resolve();
      };
      const timer = setTimeout(finish, 12000);
      connection.addEventListener('icegatheringstatechange', check);
      check();
    });
  }
  if (videoCall !== call) return;
  await api('video-call/signal', {invite: call.invite, sdp: connection.localDescription.sdp});
  call.sentDescription = true;
}

async function pollVideoCall(call) {
  try {
    const signal = await api('video-call/signal', {invite: call.invite});
    if (videoCall !== call) return;
    if (signal.joined) {
      if (!call.connectingAt) {
        call.connectingAt = Date.now();
        callElement('.call-link').hidden = true;
        callButton('copy').hidden = true;
        callStatus('Connecting video call…');
      }
      const connection = call.connection;
      if (signal.sdp && !connection.remoteDescription) {
        await connection.setRemoteDescription({type: call.join ? 'offer' : 'answer', sdp: signal.sdp});
      }
      if (videoCall !== call) return;
      if (!call.sentDescription && (!call.join || connection.remoteDescription)) {
        await publishCallDescription(call);
      }
      if ((!call.connected && Date.now() - call.connectingAt > 45000)
          || (call.disconnectedAt && Date.now() - call.disconnectedAt > 20000)) {
        finishVideoCall('The video call could not connect.');
        return;
      }
    }
    if (videoCall === call) call.timer = setTimeout(() => pollVideoCall(call), 2000);
  } catch (error) {
    if (videoCall === call) finishVideoCall(error.message);
  }
}

function finishVideoCall(message, unloading = false) {
  const call = videoCall;
  videoCall = null;
  videoCallInvite = null;
  videoCallPanel.hidden = true;
  videoCallPanel.remove();
  clearTimeout(call?.timer);
  call?.connection?.close();
  call?.stream?.getTracks().forEach(track => track.stop());
  for (const video of videoCallPanel.querySelectorAll('video')) video.srcObject = null;
  callElement('.video-call-picture').hidden = true;
  const link = callElement('.call-link');
  link.hidden = true;
  link.value = '';
  for (const action of ['accept', 'copy', 'audio', 'video', 'sound']) callButton(action).hidden = true;
  callButton('audio').textContent = 'Mute microphone';
  callButton('video').textContent = 'Turn camera off';
  if (call?.registered) {
    if (unloading) {
      const body = new Blob([JSON.stringify({invite: call.invite})], {type: 'application/json'});
      navigator.sendBeacon?.('/api/video-call/end', body);
    } else {
      api('video-call/end', {invite: call.invite}).catch(() => {});
    }
  }
  if (message) toast(message);
}

videoCallPanel.addEventListener('click', async event => {
  const action = event.target.closest('button')?.dataset.callControl;
  if (action === 'accept') await startVideoCall(true);
  if (action === 'end') {
    finishVideoCall(videoCall ? 'Video call ended.' : null);
    document.querySelector((document.querySelector('.modal') ? '.modal ' : '#app ') + '[data-action="video-call"]')?.focus();
  }
  if (action === 'copy') {
    const link = callElement('.call-link');
    try {
      await navigator.clipboard.writeText(link.value);
      toast('Video call invite copied.');
    } catch {
      link.focus();
      link.select();
    }
  }
  if (action === 'sound') {
    try {
      await callElement('.call-remote').play();
      callButton('sound').hidden = true;
    } catch {
      toast('Call audio could not start.');
    }
  }
  if (action === 'audio' || action === 'video') {
    const track = videoCall?.stream?.getTracks().find(item => item.kind === action);
    if (!track) return;
    track.enabled = !track.enabled;
    callButton(action).textContent = action === 'audio'
      ? (track.enabled ? 'Mute microphone' : 'Unmute microphone')
      : (track.enabled ? 'Turn camera off' : 'Turn camera on');
  }
});
window.addEventListener('pagehide', () => finishVideoCall(null, true));
