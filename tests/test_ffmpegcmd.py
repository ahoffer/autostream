import unittest

import ffmpegcmd


class StreamingCommandTests(unittest.TestCase):
    def destination(self, output):
        return "rtsp://localhost:8554/boats" if output == "rtsp" else "udp://receiver:40002"

    def spec(self, passthrough=False):
        return ffmpegcmd.FFmpegCommandSettings(
            loop_count=-1, gop_frames=30, frame_rate=None, passthrough=passthrough,
            audio_copy=True, bitrate_cap="2M",
        )

    def test_looping_transcode_uses_software_decode_and_selected_encoder(self):
        for output in ffmpegcmd.OUTPUT_NAMES:
            with self.subTest(output=output):
                argv = ffmpegcmd.build_argv("boats.avi", self.spec(), self.destination(output))
                input_opts = argv[:argv.index("-i")]
                self.assertNotIn("-c:v", input_opts)
                self.assertNotIn("-hwaccel", argv)
                self.assertNotIn("-init_hw_device", argv)
                self.assertEqual(argv[argv.index("-stream_loop") + 1], "-1")
                self.assertEqual(argv[argv.index("-c:v") + 1], "libx264")
                self.assertEqual(self.spec().encoder_label, "libx264 (software)")

    def test_passthrough_still_copies_video_and_udp_metadata(self):
        for output in ffmpegcmd.OUTPUT_NAMES:
            with self.subTest(output=output):
                argv = ffmpegcmd.build_argv("boats.avi", self.spec(passthrough=True), self.destination(output))
                self.assertEqual(argv[argv.index("-c:v") + 1], "copy")
                self.assertNotIn("-hwaccel", argv)
                if output == "udp":
                    self.assertIn("-copy_unknown", argv)
                    self.assertEqual(argv[argv.index("-c:d") + 1], "copy")


if __name__ == "__main__":
    unittest.main()
