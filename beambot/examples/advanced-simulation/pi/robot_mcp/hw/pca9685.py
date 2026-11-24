"""Minimal PCA9685 (16-ch PWM / servo HAT) driver over /dev/i2c-N. Stdlib only.

Enable I2C on Arch Linux ARM: add `dtparam=i2c_arm=on` to /boot/config.txt,
`echo i2c-dev > /etc/modules-load.d/i2c.conf`, reboot. Check: `i2cdetect -y 1` shows 0x40.
"""
import fcntl
import os
import time

I2C_SLAVE = 0x0703
MODE1, PRESCALE, LED0_ON_L = 0x00, 0xFE, 0x06


class PCA9685:
    def __init__(self, bus=1, address=0x40, freq_hz=50):
        self.fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE, address)
        self.freq = freq_hz
        self._write(MODE1, 0x00)
        self.set_freq(freq_hz)

    def _write(self, reg, val):
        os.write(self.fd, bytes([reg, val & 0xFF]))

    def _read(self, reg):
        os.write(self.fd, bytes([reg]))
        return os.read(self.fd, 1)[0]

    def set_freq(self, hz):
        prescale = int(round(25_000_000.0 / (4096 * hz)) - 1)
        old = self._read(MODE1)
        self._write(MODE1, (old & 0x7F) | 0x10)  # sleep
        self._write(PRESCALE, prescale)
        self._write(MODE1, old)
        time.sleep(0.005)
        self._write(MODE1, old | 0xA0)  # restart + auto-increment

    def set_pwm(self, ch, on, off):
        base = LED0_ON_L + 4 * ch
        os.write(self.fd, bytes([base, on & 0xFF, on >> 8, off & 0xFF, off >> 8]))

    def set_pulse_us(self, ch, us):
        period_us = 1_000_000.0 / self.freq
        self.set_pwm(ch, 0, int(4096 * us / period_us))

    def release(self, ch):
        """Stop sending pulses (servo goes limp)."""
        self.set_pwm(ch, 0, 0)

    def release_all(self):
        os.write(self.fd, bytes([0xFA, 0, 0, 0, 0]))  # ALL_LED registers
