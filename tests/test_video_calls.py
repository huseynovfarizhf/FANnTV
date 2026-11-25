from concurrent.futures import ThreadPoolExecutor
import time

import server
from tests_helper import AccountHarness


class VideoCallTests(AccountHarness):
    def test_signaling_is_private_and_only_one_guest_can_join(self):
        self.assertEqual(self.request('video-call/create', {})[0], 401)
        self.register()
        guests = [self.new_client(), self.new_client()]
        for index, client in enumerate(guests):
            self.register(f'guest{index}@example.com', client)
        status, room = self.request('video-call/create', {})
        self.assertEqual(status, 200)
        payload = {'invite': room['invite']}
        self.assertEqual(self.request('video-call/join', payload)[0], 409)
        self.assertEqual(self.request('video-call/signal', payload, client=guests[0])[0], 404)
        with ThreadPoolExecutor() as pool:
            results = list(pool.map(
                lambda client: self.request('video-call/join', payload, client=client)[0], guests))
        self.assertEqual(sorted(results), [200, 409])
        guest = guests[results.index(200)]
        stranger = guests[results.index(409)]
        offer = 'v=0\r\ns=offer\r\n'
        answer = 'v=0\r\ns=answer\r\n'
        self.assertEqual(self.request('video-call/end', payload, client=stranger)[0], 404)
        self.assertEqual(self.request('video-call/signal', {**payload, 'sdp': offer})[0], 200)
        self.assertEqual(self.request('video-call/signal', payload, client=guest)[1]['sdp'], offer)
        self.assertEqual(self.request('video-call/signal', {**payload, 'sdp': answer}, client=guest)[0], 200)
        self.assertEqual(self.request('video-call/signal', payload)[1]['sdp'], answer)
        self.assertEqual(self.request('video-call/end', payload, client=guest)[0], 200)
        self.assertEqual(self.request('video-call/signal', payload)[0], 404)

    def test_expired_invitation_is_removed_and_signed_out_host_ends_call(self):
        self.register()
        guest = self.new_client()
        self.register('guest@example.com', guest)
        _, room = self.request('video-call/create', {})
        with server.connect() as db:
            db.execute('UPDATE video_calls SET created=?', (time.time() - 601,))
        self.assertEqual(self.request('video-call/join', {'invite': room['invite']}, client=guest)[0], 404)
        with server.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM video_calls').fetchone()[0], 0)
        _, room = self.request('video-call/create', {})
        payload = {'invite': room['invite']}
        self.assertEqual(self.request('video-call/join', payload, client=guest)[0], 200)
        self.request('logout', {})
        self.assertEqual(self.request('video-call/signal', payload, client=guest)[0], 404)
