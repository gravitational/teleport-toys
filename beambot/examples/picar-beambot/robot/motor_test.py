"""
PiCar-X motor test — runs ON the Pi. Exercises each actuator one at a time, slowly.

Talks to the SunFounder Robot HAT directly via robot_hat_lite.py (copy both files),
so it needs only `smbus` and `gpiod` (v2) — no picarx / robot_hat install.

  python3 motor_test.py            # interactive menu
  python3 motor_test.py all        # run every test in sequence
  python3 motor_test.py left right steer

Put the car on a block so the wheels spin freely.
"""
import sys
import time

from robot_hat_lite import GRAYSCALE_ADC, PAN, STEER, TILT, Hat

SPEED = 30      # 0-100 (picarx units), keep it low for bench testing
RUN_S = 1.5

hat = Hat()


def run(action, msg):
    print(f"  {msg}")
    action()
    time.sleep(RUN_S)


def stop():
    hat.stop()
    hat.servo(STEER, 0)
    time.sleep(0.3)


def test_left():
    print("LEFT wheel")
    run(lambda: hat.motor("left", SPEED), "forward")
    run(lambda: hat.motor("left", -SPEED), "backward")
    stop()


def test_right():
    print("RIGHT wheel")
    run(lambda: hat.motor("right", SPEED), "forward")
    run(lambda: hat.motor("right", -SPEED), "backward")
    stop()


def test_drive():
    print("BOTH wheels")
    run(lambda: hat.drive(SPEED), "forward")
    run(lambda: hat.drive(-SPEED), "backward")
    stop()


def test_ramp():
    print("RAMP both wheels 0 -> 50 -> 0")
    for s in list(range(0, 51, 5)) + list(range(50, -1, -5)):
        hat.drive(s)
        print(f"  speed {s}", end="\r")
        time.sleep(0.2)
    print()
    stop()


def sweep(channel, name, lo, hi):
    print(f"{name} servo {lo}..{hi}")
    for a in [0, lo, 0, hi, 0]:
        hat.servo(channel, a)
        print(f"  {a:+d} deg")
        time.sleep(0.6)


def test_steer():
    sweep(STEER, "STEERING", -30, 30)


def test_pan():
    sweep(PAN, "CAMERA PAN", -90, 90)


def test_tilt():
    sweep(TILT, "CAMERA TILT", -35, 65)


def test_sensors():
    print("SENSORS (5 reads)")
    for _ in range(5):
        gray = [hat.adc(c) for c in GRAYSCALE_ADC]
        print(f"  ultrasonic={hat.ultrasonic_cm()} cm  grayscale={gray}")
        time.sleep(0.3)


TESTS = {
    "left": test_left, "right": test_right, "drive": test_drive, "ramp": test_ramp,
    "steer": test_steer, "pan": test_pan, "tilt": test_tilt, "sensors": test_sensors,
}


def menu():
    names = list(TESTS)
    while True:
        print("\n" + "  ".join(f"[{i + 1}] {n}" for i, n in enumerate(names)) + "  [a] all  [q] quit")
        c = input("> ").strip().lower()
        if c == "q":
            return
        if c == "a":
            for fn in TESTS.values():
                fn()
        elif c.isdigit() and 1 <= int(c) <= len(names):
            TESTS[names[int(c) - 1]]()
        elif c in TESTS:
            TESTS[c]()


def main(args):
    try:
        if not args:
            menu()
        for name in (TESTS if args == ["all"] else args):
            if name not in TESTS:
                sys.exit(f"unknown test {name!r}; choose from: all {' '.join(TESTS)}")
            TESTS[name]()
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        stop()
        hat.servo(PAN, 0)
        hat.servo(TILT, 0)
        print("motors stopped")


if __name__ == "__main__":
    main(sys.argv[1:])
