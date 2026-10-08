"""Small regression tests for the routing safety checks (standard library only)."""
import unittest
from route_svg import check_endpoint, crosses, rounded_path, topology, wire_metrics

class RouteChecks(unittest.TestCase):
    def test_reject_diagonal_and_duplicate_segments(self):
        for points in ([[0,0],[1,1]], [[0,0],[0,0]], [[0,0]]):
            with self.assertRaises(ValueError):
                rounded_path(points)

    def test_boundary_ports(self):
        box = (10,20,100,80)
        for point in ((10,60),(110,60),(60,20),(60,100)):
            check_endpoint(point,box,'node')
        for point in ((60,60),(0,60),(10,101)):
            with self.assertRaises(ValueError):
                check_endpoint(point,box,'node')

    def test_scoped_topology(self):
        self.assertEqual(topology('app.(A.drv -> B.vimu)[0]'),('app.A.drv','app.B.vimu'))
        self.assertEqual(topology('map.(a <-> b)[0]'),('map.a','map.b'))

    def test_leaf_box_obstacles(self):
        box = (10,10,20,20)
        self.assertTrue(crosses((0,20),(40,20),box))
        self.assertTrue(crosses((20,0),(20,40),box))
        self.assertFalse(crosses((0,10),(40,10),box))
        self.assertFalse(crosses((0,20),(10,20),box))

    def test_wire_crossings_and_shared_segments_are_distinct(self):
        a = {'edge':'a','points':[[0,10],[20,10]]}
        b = {'edge':'b','points':[[10,0],[10,20]]}
        c = {'edge':'c','points':[[5,10],[15,10]]}
        count, overlaps = wire_metrics([a,b])
        self.assertEqual(count,1)
        self.assertEqual(overlaps,[])
        count, overlaps = wire_metrics([a,c])
        self.assertEqual(count,0)
        self.assertEqual(overlaps[0]['length'],10)
        b['role']='lifeline'
        self.assertEqual(wire_metrics([a,b]),(0,[]))

if __name__ == '__main__':
    unittest.main()
