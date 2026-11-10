import unittest

from shop.receipt import Receipt


class HiddenTaxTest(unittest.TestCase):
    def receipt(self):
        r = Receipt()
        for name in ("pen", "pad", "ink"):
            r.add(name, 199)  # 199 * 1.0825 = 215.42 per line; 597 * 1.0825 = 646.25 in total
        return r

    def test_total_is_the_sum_of_the_printed_lines(self):
        r = self.receipt()
        lines = r.lines_with_tax()
        self.assertEqual(len(lines), 3)
        self.assertEqual(r.total_with_tax(), sum(cents for _, cents in lines))

    def test_each_line_rounds_to_the_cent(self):
        self.assertEqual([c for _, c in self.receipt().lines_with_tax()], [215, 215, 215])
