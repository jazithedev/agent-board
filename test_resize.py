import unittest
from unittest import mock

import agent_board as ab


class FakeScreen:
    def __init__(self, rows: int, cols: int) -> None:
        self.size = (rows, cols)
        self.cleared = False

    def getmaxyx(self):
        return self.size

    def clearok(self, flag) -> None:
        self.cleared = flag


class SyncSize(unittest.TestCase):
    def test_follows_a_terminal_that_grew_without_a_resize_key(self) -> None:
        # Arrange
        scr = FakeScreen(30, 80)

        # Act
        with mock.patch.object(ab.os, "get_terminal_size", return_value=(160, 30)), \
                mock.patch.object(ab.curses, "resizeterm") as resize:
            ab.sync_size(scr)

        # Assert
        resize.assert_called_once_with(30, 160)
        self.assertTrue(scr.cleared)

    def test_leaves_a_matching_size_alone(self) -> None:
        # Arrange
        scr = FakeScreen(30, 80)

        # Act
        with mock.patch.object(ab.os, "get_terminal_size", return_value=(80, 30)), \
                mock.patch.object(ab.curses, "resizeterm") as resize:
            ab.sync_size(scr)

        # Assert
        resize.assert_not_called()
        self.assertFalse(scr.cleared)


if __name__ == "__main__":
    unittest.main()
