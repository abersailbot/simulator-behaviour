from __future__ import print_function
from abc import ABCMeta, abstractmethod
import time
import math
import sys

import boatdclient
from boatdclient import Bearing


def mirror_angle(angle):
    angle = float(angle)
    if angle > 180:
        return 180 - (angle % 180)
    else:
        return angle


def map_range(x, in_min, in_max, out_min, out_max):
    return (x - in_min) * (out_max - out_min) / (in_max - in_min) + out_min


def output(*args):
    print('\033c\n')
    for k, v in zip(args[::2], args[1::2]):
        print('\t{}\t| {}'.format(k.ljust(20), v))


class Navigator(object):
    '''
    Abstract class used to implement behaviours.

    This should be inherited from and ``check_new_target`` defined to create a
    behaviour with some targets. See ``demo-waypoint-behaviour`` for an example
    of basic waypoint targeting.
    '''
    __metaclass__ = ABCMeta

    def __init__(self,
                 enable_tacking=True,
                 enable_cross_track_minimization=True,
                 enable_emergency_maneuver=True):
        self.enable_tacking = enable_tacking
        self.enable_cross_track_minimization = enable_cross_track_minimization
        self.enable_emergency_maneuver = enable_emergency_maneuver

        self.boat = boatdclient.Boat(auto_update=False)

        self.target = None
        self.prev_target = None

        # how long the rudder can be hardover for before trying to snap the boat
        # out of it in an emergency
        self.hardover_rudder_timeout = 20

        # how far over the rudder can be before we assume it's hardover in and emergency
        self.hardover_rudder_threshold = 40

        self.rudder_angle = 0
        self.k_p = 0.35
        self.k_i = 0.001
        self.integrator = 0
        self.integrator_max = 1000

        # tracks the last time the the rudder was in a good position (i.e. not hard over)
        self.last_time_rudder_not_maxed = time.time()

        self.tacking_left = None
        self.tacking_right = None
        self.last_tack_time = 0
        self.cone_angle = Bearing(20)
        self.tacking_angle = Bearing(50)

        # maximum rudder angle on either side
        self.rudder_angle_max = 45

        self.cross_track_error = 0

        self.next_log_time = 0

    def override_rudder(self, rudder_angle):
        timeout = time.time() + 10
        initial_heading = self.boat.heading

        rudder_angle = -45 if rudder_angle > 0 else 45
        self.boat.set_rudder(rudder_angle)
        while time.time() < timeout and \
                abs(initial_heading.delta(self.boat.heading)) < 170:
            time.sleep(0.1)

    def set_target(self, value):
        '''Set the target angle for the boat.'''
        self.target = value
        self.integrator = 0

    def update(self):
        '''Update actuators to make progress towards target.'''

        # this currently always assumes that self.target will return a long/lat
        # point
        current_heading = self.boat.heading
        if isinstance(self.target, boatdclient.Point):
            target_heading = self.boat.position.bearing_to(self.target)
        else:
            target_heading = self.target

        if self.enable_cross_track_minimization:
            if isinstance(self.prev_target, boatdclient.Point) and isinstance(self.target, boatdclient.Point):
                # TODO find ideal constant to properly scale up/down effects of cross track error
                self.cross_track_error = self.boat.position.cross_track_distance(self.prev_target, self.target) * 5
            else:
                self.cross_track_error = 0

        modulus_to_wind = 0

        #BUG: desired heading not being set correctly
        if time.time() - self.last_tack_time > 10:
            # tacking logic
            if abs(target_heading.delta(self.boat.wind.absolute)) <=\
            self.tacking_angle and self.enable_tacking:
                bearing_to_wind = self.boat.position.bearing_to(self.target) -\
                                self.boat.wind.absolute

                # choose the best initial tack, based on which side of the cone
                # we're on
                if self.tacking_right is None or self.tacking_left is None:
                    self.last_tack_time=time.time()
                    if bearing_to_wind <= 180:
                        self.tacking_right = True
                        self.tacking_left = False
                    else:
                        self.tacking_right = False
                        self.tacking_left = True
                # just between 0 and 180 degrees, needed to reduce if statements as cone is reflected
                modulus_to_wind = mirror_angle(bearing_to_wind)
                #print("bearing to wind =",bearing_to_wind,"modulus_to_wind=",modulus_to_wind,"cone=",float(self.cone_angle))

                # detect if the boat is outside cone
                if modulus_to_wind >= float(self.cone_angle):
                    #print("outside tacking cone")
                    if bearing_to_wind <= 180:
                        target_heading = self.boat.wind.absolute + \
                                        self.tacking_angle
                        if self.tacking_left == True and self.tacking_right == False:
                            self.reset_integral()
                            self.last_tack_time=time.time()
                            print("choosing right tack",time.time())
                        self.tacking_right = True
                        self.tacking_left = False
                        target_heading = self.boat.wind.absolute + \
                                        self.tacking_angle

                    if bearing_to_wind > 180:
                        target_heading = self.boat.wind.absolute - \
                                        self.tacking_angle
                        self.reset_integral()
                        if self.tacking_left == False and self.tacking_right == True:
                            self.reset_integral()
                            self.last_tack_time=time.time()
                            print("choosing left tack",time.time())

                        self.tacking_right = False
                        self.tacking_left = True
                        target_heading = self.boat.wind.absolute - \
                                        self.tacking_angle
                # else the boat is inside cone
                else:
                    if self.tacking_left is True:
                        target_heading = self.boat.wind.absolute - \
                                        self.tacking_angle
                    if self.tacking_right is True:
                        target_heading = self.boat.wind.absolute + \
                                        self.tacking_angle
            else:
                if self.tacking_left != None and self.tacking_right != None:
                    last_tack_time=time.time()
                    print("not tacking anymore",time.time())
                self.tacking_left = None
                self.tacking_right = None


        # FIXME check if both values are of the correct sign with respect to
        # eachother
        #print("error=",current_heading.delta(target_heading))
        #error = current_heading.delta(target_heading) - self.cross_track_error
        error = current_heading.delta(target_heading)
        #print("error2=",error)

        # only integrate if the rudder is not at maximum position
        if -self.rudder_angle_max < self.rudder_angle < self.rudder_angle_max:
            self.integrator += self.k_i * error
            if self.integrator > self.integrator_max:
                self.integrator = self.integrator_max
            elif self.integrator < -self.integrator_max:
                self.integrator = -self.integrator_max

        rudder_angle = -(self.k_p * error + self.k_i * self.integrator)

        if rudder_angle > self.rudder_angle_max:
            rudder_angle = self.rudder_angle_max
        if rudder_angle < -self.rudder_angle_max:
            rudder_angle = -self.rudder_angle_max

        self.rudder_angle = rudder_angle

        # emergency procedure to get the boat to turn the opposite direction
        # when stuck trying to turn towards a target heading
        if self.enable_emergency_maneuver:
            if abs(rudder_angle) < self.hardover_rudder_threshold:
                self.last_time_rudder_not_maxed = time.time()
            elif time.time() - self.last_time_rudder_not_maxed > self.hardover_rudder_timeout:
                self.override_rudder(rudder_angle)

                # allow 60 seconds to recover from the maneuver
                self.last_time_rudder_not_maxed = time.time() + 60

        sail_angle = self.choose_sail_angle()

        self.boat.set_rudder(rudder_angle)
        self.boat.set_sail(sail_angle)

        # output some debug information
        if self.next_log_time <= time.time():
            sys.stdout.flush()
            self.next_log_time = time.time() + 1
            distance = self.boat.position.distance_to(self.target)

            print("dist",'{:.1f}'.format(distance),
                "wpnum",getattr(self, 'current_point'),
                "lat",'{:.4f}'.format(self.boat.position[0]),
                "lon",'{:.4f}'.format(self.boat.position[1]),
                'tlat', '{:.4f}'.format(self.target[0]),
                'tlon', '{:.4f}'.format(self.target[1]),
                'hdg', int(current_heading),
                'deshdg', int(target_heading),
                'hdgerr', int(error),
                'xte', '{:.1f}'.format(self.cross_track_error),
                'int', '{:.1f}'.format(self.integrator),
                'rud', '{:.1f}'.format(rudder_angle),
                'rwind', int(self.boat.wind.apparent),
                'twind', int(self.boat.wind.absolute),
                'sail', '{:.1f}'.format(sail_angle),
                'tackl', self.tacking_left,
                'tackr', self.tacking_right,
                'windmod', int(modulus_to_wind))
        sys.stdout.flush()


    #print("time=",time.time(),self.next_log_time)

    def choose_sail_angle(self):
        '''
        Return the correct angle to set the sail based on current wind
        direction.
        '''

        apparent_wind = float(self.boat.wind.apparent + 180)

        if apparent_wind > 180:
            semicircle_wind = 360 - apparent_wind
        else:
            semicircle_wind = apparent_wind

        # linear offset for sail angle output
        sail_offset = 0

        # maximum and minimum output angles for sail
        min_sail_angle = 1
        max_sail_angle = 50

        if semicircle_wind < 45:
            semicircle_wind = 45
        elif semicircle_wind > 135:
            semicircle_wind = 135

        sail_angle = map_range(semicircle_wind, 45, 135,
                               min_sail_angle, max_sail_angle) + sail_offset

        return 50 - sail_angle

    def reset_integral(self):
        '''
        reset the integrator, useful for doing after reaching a waypoint
        '''
        self.integrator = 0

    def run(self):
        '''
        Run the main loop for the behaviour.
        '''
        while True:
            time1 = time.time()

            self.boat.update()
            target = self.check_new_target()
            if target is not None:
                self.prev_target = self.target
                self.set_target(target)

            self.update()

            # FIXME: remove this timing information after
            # https://github.com/boatd/boatd/issues/68 is somewhat completed
            time2 = time.time()
            #with open('timing', 'a') as f:
            #    f.write('{}\n'.format(time2-time1))

    @abstractmethod
    def check_new_target(self):
        '''
        Check if a new target point needs to be selected.

        Return a new ``Point`` or ``Bearing` if target will be changed,
        ``None`` otherwise.
        '''
        pass
