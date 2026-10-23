import unittest

import ffmpegcmd
from mediastream import Delivery


class StreamingCommandTests(unittest.TestCase):
    def spec(self, passthrough=False):
        return ffmpegcmd.FFmpegCommandSettings(
            loop_count=-1, gop_frames=30, frame_rate=None, passthrough=passthrough,
            audio_copy=True, bitrate_cap="2M",
        )

    def test_looping_transcode_uses_software_decode_and_selected_encoder(self):
        for delivery in (Delivery("rtsp://localhost:8554/boats", "rtsp", False), Delivery("udp://receiver:40002")):
            with self.subTest(muxer=delivery.muxer):
                argv = ffmpegcmd.build_argv("boats.avi", self.spec(), delivery)
                input_opts = argv[:argv.index("-i")]
                self.assertNotIn("-c:v", input_opts)
                self.assertNotIn("-hwaccel", argv)
                self.assertEqual(argv[argv.index("-stream_loop") + 1], "-1")
                self.assertEqual(argv[argv.index("-c:v") + 1], "libx264")
                self.assertEqual(self.spec().encoder_label, "libx264 (software)")

    def test_rtsp_maps_video_and_audio_only_while_transport_streams_carry_everything(self):
        rtsp = ffmpegcmd.build_argv("boats.avi", self.spec(passthrough=True), Delivery("rtsp://localhost:8554/boats", "rtsp", False))
        self.assertEqual(rtsp[argv_index(rtsp, "-c:v") + 1], "copy")
        self.assertNotIn("-copy_unknown", rtsp)
        self.assertEqual(rtsp[-4:], ["-f", "rtsp", "-rtsp_transport", "tcp"][:0] + rtsp[-4:])
        self.assertEqual(rtsp[-5:], ["-f", "rtsp", "-rtsp_transport", "tcp", "rtsp://localhost:8554/boats"])
        udp = ffmpegcmd.build_argv("boats.avi", self.spec(passthrough=True), Delivery("udp://receiver:40002?pkt_size=1316"))
        self.assertIn("-copy_unknown", udp)
        self.assertEqual(udp[udp.index("-c:d") + 1], "copy")
        self.assertEqual(udp[-3:], ["-f", "mpegts", "udp://receiver:40002?pkt_size=1316"])

    def test_each_muxer_gets_its_own_tail(self):
        rtp = ffmpegcmd.build_argv("boats.avi", self.spec(), Delivery("rtp://receiver:40022", "rtp_mpegts"))
        self.assertEqual(rtp[-3:], ["-f", "rtp_mpegts", "rtp://receiver:40022"])
        self.assertNotIn("-max_interleave_delta", rtp)
        self.assertIn("-max_interleave_delta", ffmpegcmd.build_argv("boats.avi", self.spec(), Delivery("udp://receiver:40002")))
        http = ffmpegcmd.build_argv("boats.avi", self.spec(), Delivery("http://0.0.0.0:43000", "mpegts", extra_args=("-listen", "1")))
        self.assertEqual(http[-5:], ["-listen", "1", "-f", "mpegts", "http://0.0.0.0:43000"])
        srtp = ffmpegcmd.build_argv("boats.avi", self.spec(), Delivery(
            "srtp://receiver:40060", "rtp_mpegts", extra_args=("-srtp_out_suite", "AES_CM_128_HMAC_SHA1_80", "-srtp_out_params", "a2V5")))
        self.assertEqual(srtp[-7:], ["-srtp_out_suite", "AES_CM_128_HMAC_SHA1_80", "-srtp_out_params", "a2V5",
                                     "-f", "rtp_mpegts", "srtp://receiver:40060"])
        hls = ffmpegcmd.build_argv("boats.avi", self.spec(), Delivery("/app/hls/boats/index.m3u8", "hls"))
        self.assertEqual(hls[hls.index("-f") + 1], "hls")
        self.assertEqual(hls[hls.index("-hls_segment_type") + 1], "mpegts")
        self.assertEqual(hls[hls.index("-hls_segment_filename") + 1], "/app/hls/boats/seg%05d.ts")
        self.assertEqual(hls[-1], "/app/hls/boats/index.m3u8")
        self.assertIn("-copy_unknown", hls)

    def test_prepare_reports_the_source_tracks_and_no_command_without_a_url(self):
        profile = ffmpegcmd.MediaProbeResult(('h264',), ('aac',), True, 'yuv420p', 4000000, 30., ('klv',))
        prepared = ffmpegcmd._command_for_profile("boats.ts", {'loop_count': -1}, Delivery(None, blocker='no port'), profile)
        self.assertEqual(prepared['argv'], ())
        self.assertEqual((prepared['video_codecs'], prepared['audio_codecs'], prepared['data_codecs']), (['h264'], ['aac'], ['klv']))
        self.assertEqual(prepared['effective_mode'], 'passthrough')


def argv_index(argv, flag):
    return argv.index(flag)


if __name__ == "__main__":
    unittest.main()
