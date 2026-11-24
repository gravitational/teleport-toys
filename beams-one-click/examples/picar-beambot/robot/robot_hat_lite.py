"""
Minimal SunFounder Robot HAT driver for the PiCar-X, without picarx / robot_hat installed.

Talks to the HAT's MCU at I2C 0x14 (same register map as robot_hat) and drives the motor direction
and ultrasonic pins over GPIO with gpiod v2. Camera frames come from libcamera via GStreamer.

  Hat        low-level: servo(), motor(), ultrasonic_cm(), adc()
  HatCar     the subset of picarx.Picarx that robot_mcp.py uses
  GstCamera  latest JPEG frame from the CSI camera (gst-launch-1.0 + libcamerasrc)

Needs: smbus, gpiod>=2 (python), gstreamer + gst-plugin-libcamera + gst-plugins-good for the camera.
"""
import os
import shlex
import subprocess
import threading
import time

HAT_ADDR = 0x14
REG_CHN, REG_PSC, REG_ARR = 0x20, 0x40, 0x44   # pulse width per channel, prescaler/period per timer
CLOCK = 72_000_000
SERVO_PERIOD = 4095
MOTOR_PERIOD, MOTOR_PSC = 4095, 10             # ~1.7 kHz motor PWM

# beambot wiring: camera pan/tilt are swapped vs. stock picarx (P0 = tilt, P1 = pan).
TILT = int(os.environ.get("CAM_TILT_CH", "0"))
PAN = int(os.environ.get("CAM_PAN_CH", "1"))
STEER = int(os.environ.get("STEER_CH", "2"))
LEFT_PWM, RIGHT_PWM = 13, 12                   # P13, P12 (timer 3)
LEFT_DIR, RIGHT_DIR = 23, 24                   # D4, D5 (BCM)
US_TRIG, US_ECHO = 27, 22                      # D2, D3 (BCM)
SPK_EN = 20                                    # Robot HAT V4 speaker amp switch (BCM); audio is I2S (hifiberry-dac)
GRAYSCALE_ADC = (0, 1, 2)                      # A0..A2


def _gpiomem_output(pin, high):
    """Force a BCM GPIO to output and drive it through /dev/gpiomem (BCM2835..2711 register layout, Pi 1-4).
    Equivalent to `pinctrl set <pin> op dh|dl`. Needs read/write on /dev/gpiomem (udev rule, see README)."""
    import mmap
    import struct
    fd = os.open("/dev/gpiomem", os.O_RDWR | os.O_SYNC)
    try:
        m = mmap.mmap(fd, 4096)
        try:
            fsel, shift = (pin // 10) * 4, (pin % 10) * 3          # GPFSELn: 3 bits per pin, 001 = output
            v = struct.unpack_from("<I", m, fsel)[0]
            struct.pack_into("<I", m, fsel, (v & ~(0b111 << shift)) | (0b001 << shift))
            struct.pack_into("<I", m, 0x1C if high else 0x28, 1 << pin)   # GPSET0 / GPCLR0
        finally:
            m.close()
    finally:
        os.close(fd)


class Hat:
    def __init__(self):
        import gpiod
        import smbus
        from gpiod.line import Direction, Value
        self._on, self._off = Value.ACTIVE, Value.INACTIVE
        self.bus = smbus.SMBus(1)
        out = gpiod.LineSettings(direction=Direction.OUTPUT)
        try:
            self.gpio = gpiod.request_lines(
                "/dev/gpiochip0", consumer="robot_hat_lite",
                config={(LEFT_DIR, RIGHT_DIR, US_TRIG, SPK_EN): out, US_ECHO: gpiod.LineSettings(direction=Direction.INPUT)})
            self.speaker_via = "gpiod"
        except (OSError, ValueError):
            # GPIO 20 is also PCM_DIN: with the I2S sound card overlay the kernel owns it (EINVAL here), exactly as on
            # a stock PiCar-X. SunFounder's library forces it to an output anyway (`pinctrl set 20 op dh`); the DAC
            # never drives DIN, so nothing is lost. _gpiomem_output does the same register poke.
            self.gpio = gpiod.request_lines(
                "/dev/gpiochip0", consumer="robot_hat_lite",
                config={(LEFT_DIR, RIGHT_DIR, US_TRIG): out, US_ECHO: gpiod.LineSettings(direction=Direction.INPUT)})
            self.speaker_via = "gpiomem"
        self._write(REG_PSC + 0, round(CLOCK / 50 / SERVO_PERIOD) - 1)   # timer 0: 50 Hz servos
        self._write(REG_ARR + 0, SERVO_PERIOD)
        self._write(REG_PSC + 3, MOTOR_PSC - 1)                          # timer 3: motors
        self._write(REG_ARR + 3, MOTOR_PERIOD - 1)

    def _write(self, reg, value):
        self.bus.write_i2c_block_data(HAT_ADDR, reg, [value >> 8 & 0xFF, value & 0xFF])

    def servo(self, channel, angle):
        us = 500 + (max(-90, min(90, angle)) + 90) / 180 * 2000
        self._write(REG_CHN + channel, int(us / 20000 * SERVO_PERIOD))

    def motor(self, side, speed):
        """side: 'left'/'right'; speed -100..100, positive = wheel rolls forward."""
        pwm, pin = (LEFT_PWM, LEFT_DIR) if side == "left" else (RIGHT_PWM, RIGHT_DIR)
        if side == "right":
            speed = -speed                       # right motor is mounted mirrored
        duty = 0 if speed == 0 else min(100, abs(speed) // 2 + 50)   # motors stall below ~50%
        self.gpio.set_value(pin, self._on if speed < 0 else self._off)
        self._write(REG_CHN + pwm, int(duty / 100 * MOTOR_PERIOD))

    def drive(self, speed):
        self.motor("left", speed)
        self.motor("right", speed)

    def speaker(self, on=True):
        if self.speaker_via == "gpiod":
            self.gpio.set_value(SPK_EN, self._on if on else self._off)
        else:
            _gpiomem_output(SPK_EN, on)

    def stop(self):
        self.drive(0)

    def ultrasonic_cm(self, timeout=0.02):
        self.gpio.set_value(US_TRIG, self._off)
        time.sleep(0.00002)
        self.gpio.set_value(US_TRIG, self._on)
        time.sleep(0.00001)
        self.gpio.set_value(US_TRIG, self._off)
        t0 = time.perf_counter()
        while self.gpio.get_value(US_ECHO) == self._off:
            if time.perf_counter() - t0 > timeout:
                return -1
        start = time.perf_counter()
        while self.gpio.get_value(US_ECHO) == self._on:
            if time.perf_counter() - start > timeout:
                return -1
        return round((time.perf_counter() - start) * 34300 / 2, 1)

    def adc(self, chn):
        self.bus.write_i2c_block_data(HAT_ADDR, 0x10 | (7 - chn), [0, 0])
        return self.bus.read_byte(HAT_ADDR) << 8 | self.bus.read_byte(HAT_ADDR)


class HatCar:
    """Drop-in for the picarx.Picarx calls robot_mcp.py makes."""

    def __init__(self):
        self.hat = Hat()
        self.ultrasonic = self                   # robot_mcp calls car.ultrasonic.read()

    def forward(self, speed): self.hat.drive(speed)
    def backward(self, speed): self.hat.drive(-speed)
    def stop(self): self.hat.stop()
    def set_dir_servo_angle(self, a): self.hat.servo(STEER, a)
    def set_cam_pan_angle(self, a): self.hat.servo(PAN, a)
    def set_cam_tilt_angle(self, a): self.hat.servo(TILT, a)
    def get_grayscale_data(self): return [self.hat.adc(c) for c in GRAYSCALE_ADC]
    def speaker(self, on=True): self.hat.speaker(on)

    def read(self):
        d = self.hat.ultrasonic_cm()
        return d if d > 0 else None


GST_PIPELINE = os.environ.get(
    "GST_PIPELINE",
    "libcamerasrc ! video/x-raw,width=640,height=480,framerate=15/1 ! videoconvert ! jpegenc quality=70 ! fdsink fd=1")


class GstCamera:
    """Runs a GStreamer pipeline that writes concatenated JPEGs to stdout; keeps the latest one."""

    def __init__(self, pipeline=GST_PIPELINE):
        self.cmd = ["gst-launch-1.0", "-q", *shlex.split(pipeline)]
        self.frame = b""
        self.error = ""
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                p = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                buf = b""
                while chunk := p.stdout.read1(65536):
                    buf += chunk
                    # jpegenc output: SOI ... EOI per frame; 0xFFD9 can't appear inside entropy-coded data
                    while (end := buf.find(b"\xff\xd9")) != -1:
                        start = buf.rfind(b"\xff\xd8", 0, end)
                        if start != -1:
                            self.frame = buf[start:end + 2]
                        buf = buf[end + 2:]
                p.wait()
                self.error = f"camera pipeline exited: {p.stderr.read().decode()[-200:].strip()}"
            except Exception as e:  # noqa: BLE001
                self.error = f"camera error: {e}"
            time.sleep(2)
