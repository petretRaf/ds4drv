import fcntl
import itertools

from evdev import InputDevice
from pyudev import Context, Monitor
from time import sleep

from ..backend import Backend
from ..exceptions import DeviceError
from ..device import DS4Device
from ..utils import zero_copy_slice


IOC_RW = 3221243904
HIDIOCSFEATURE = lambda size: IOC_RW | (0x06 << 0) | (size << 16)
HIDIOCGFEATURE = lambda size: IOC_RW | (0x07 << 0) | (size << 16)


class HidrawDS4Device(DS4Device):
    def __init__(self, name, addr, type, hidraw_device, event_device):
        try:
            self.fd = open(hidraw_device, "rb+", 0)
            self.input_device = InputDevice(event_device)
            self.input_device.grab()
        except (OSError, IOError) as err:
            raise DeviceError(err)

        self.buf = bytearray(self.report_size)

        super(HidrawDS4Device, self).__init__(name, addr, type)

    def read_report(self):
        ret = self.fd.readinto(self.buf)

        # Disconnection
        if ret == 0:
            return

        # Invalid report size, just ignore it
        if ret < self.report_size:
            return False

        buf = self.get_trimmed_report_data()

        return self.parse_report(buf)

    def read_feature_report(self, report_id, size):
        op = HIDIOCGFEATURE(size + 1)
        buf = bytearray(size + 1)
        buf[0] = report_id

        return fcntl.ioctl(self.fd, op, bytes(buf))

    def get_trimmed_report_data(self):
        raise NotImplementedError

    def write_report(self, report_id, data):
        if self.type == "bluetooth":
            # TODO: Add a check for a kernel that supports writing
            # output reports when such a kernel has been released.
            return

        hid = bytearray((report_id,))
        self.fd.write(hid + data)

    def close(self):
        try:
            self.fd.close()
            self.input_device.ungrab()
        except IOError:
            pass

    @property
    def report_size(self):
        raise NotImplementedError


class HidrawBluetoothDS4Device(HidrawDS4Device):
    __type__ = "bluetooth"

    def get_trimmed_report_data(self):
        # Cut off bluetooth data
        return zero_copy_slice(self.buf, 2)

    def set_operational(self):
        self.read_feature_report(0x02, 37)

    @property
    def report_size(self):
        return 78


class HidrawUSBDS4Device(HidrawDS4Device):
    __type__ = "usb"

    def get_trimmed_report_data(self):
        return self.buf

    def set_operational(self):
        # Get the bluetooth MAC and set operational with a single report
        addr = self.read_feature_report(0x81, 6)[1:]
        addr = ["{0:02x}".format(c) for c in bytearray(addr)]
        addr = ":".join(reversed(addr)).upper()

        self.device_name = "{0} {1}".format(addr, self.device_name)
        self.device_addr = addr

    @property
    def report_size(self):
        return 64


HID_DEVICES = {
    "Sony Computer Entertainment Wireless Controller": HidrawUSBDS4Device,
    "Wireless Controller": HidrawBluetoothDS4Device,
}


class HidrawBackend(Backend):
    __name__ = "hidraw"

    def setup(self):
        pass

    def _get_future_devices(self, context):
        """Return a generator yielding new devices."""
        monitor = Monitor.from_netlink(context)
        monitor.filter_by("hidraw")
        monitor.start()

        self._scanning_log_message()
        for device in iter(monitor.poll, None):
            if device.action == "add":
                # Sometimes udev rules has not been applied at this point,
                # causing permission denied error if we are running in user
                # mode. With this sleep this will hopefully not happen.
                sleep(1)

                yield device
                self._scanning_log_message()

    def _scanning_log_message(self):
        self.logger.info("Scanning for devices")

    @property
    def devices(self):
        """Wait for new DS4 devices to appear."""
        context = Context()

        existing_devices = context.list_devices(subsystem="hidraw")
        future_devices = self._get_future_devices(context)

        for hidraw_device in itertools.chain(existing_devices, future_devices):
            hid_device = hidraw_device.parent
            if hid_device.subsystem != "hid":
                continue

            cls = HID_DEVICES.get(hid_device.get("HID_NAME"))
            if not cls:
                continue

            for child in hid_device.parent.children:
                event_device = child.get("DEVNAME", "")
                if event_device.startswith("/dev/input/event"):
                    break
            else:
                continue


            try:
                device_addr = hid_device.get("HID_UNIQ", "").upper()
                if device_addr:
                    device_name = "{0} {1}".format(device_addr,
                                                   hidraw_device.sys_name)
                else:
                    device_name = hidraw_device.sys_name

                yield cls(name=device_name,
                          addr=device_addr,
                          type=cls.__type__,
                          hidraw_device=hidraw_device.device_node,
                          event_device=event_device)

            except DeviceError as err:
                self.logger.error("Unable to open DS4 device: {0}", err)
