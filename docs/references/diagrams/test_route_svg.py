#!/usr/bin/env python3
"""Regression tests for routing geometry; no graphics packages required."""
import unittest
from route_svg import port,points,intersects,wire_metrics,topology
class RoutingTests(unittest.TestCase):
 def test_ports_and_fraction_validation(self):
  self.assertEqual(port((10,20,100,80),('e',.25)),(110,40))
  with self.assertRaises(ValueError):port((0,0,1,1),('e',1.1))
 def test_topology_namespace(self):self.assertEqual(topology('layer.(a -> b)[0]'),('layer.a','layer.b'))
 def test_orthogonal_corridor(self):
  bs={'a':(0,0,100,100),'b':(200,200,100,100)}
  c={'source':'a','target':'b','from':['e',.5],'to':['w',.5],'mode':'mid-x'}
  self.assertEqual(points(c,bs),[(100,50),(150,50),(150,250),(200,250)])
  c['mode']='straight'
  with self.assertRaises(ValueError):points(c,bs)
 def test_block_collision_and_tangent(self):
  self.assertTrue(intersects((0,50),(200,50),(40,40,20,20)))
  self.assertFalse(intersects((0,40),(200,40),(40,40,20,20)))
 def test_collinear_segments_detected(self):
  cross,overlap=wire_metrics([{'points':[(0,10),(40,10)]},{'points':[(20,10),(60,10)]}])
  self.assertFalse(cross);self.assertEqual(overlap[0]['length'],20)
 def test_crossing_not_junction(self):
  cross,overlap=wire_metrics([{'points':[(0,20),(40,20)]},{'points':[(20,0),(20,40)]}])
  self.assertEqual(len(cross),1);self.assertFalse(overlap)
 def test_distinct_parallel_lines(self):
  self.assertEqual(wire_metrics([{'points':[(0,10),(40,10)]},{'points':[(0,30),(40,30)]}]),([],[]))
if __name__=='__main__':unittest.main()
