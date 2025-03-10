from __future__ import annotations

import copy
import logging
import math
import random
from dataclasses import dataclass
from enum import Enum
from itertools import combinations
from typing import Dict, List, Optional, Tuple, Union

import shapely
from machine_common_sense.config_manager import (
    FloorTexturesConfig,
    Vector2dInt,
    Vector3d
)

from generator import (
    MAX_TRIES,
    MaterialTuple,
    ObjectBounds,
    SceneObject,
    geometry,
    gravity_support_objects,
    instances,
    intuitive_physics_objects,
    materials,
    mechanisms,
    occluders,
    specific_objects,
    structures
)
from generator.intuitive_physics_util import (
    COLLISION_SPEEDS,
    MAX_TARGET_Z,
    MAX_TARGET_Z_STRAIGHT_ACROSS,
    MIN_TARGET_Z,
    NON_COLLISION_ANGLED_SPEEDS,
    NON_COLLISION_SPEEDS,
    TOSS_SPEEDS,
    find_off_screen_position_diagonal_away,
    find_off_screen_position_diagonal_toward,
    find_position_behind_occluder_diagonal_away,
    find_position_behind_occluder_diagonal_toward,
    retrieve_off_screen_position_x,
    retrieve_off_screen_position_y
)
from generator.movements import BASE_MOVE_LIST, TOSS_MOVE_LIST
from generator.occluders import (
    create_notched_occluder,
    occluder_gap_positioning
)
from generator.scene import PartitionFloor, Scene

from .choosers import (
    SOCCER_BALL_SCALE_MAX,
    SOCCER_BALL_SCALE_MIN,
    choose_material_tuple_from_material,
    choose_position,
    choose_random,
    choose_rotation,
    choose_shape_material
)
from .defs import (
    TARGET_LABEL,
    ILEConfigurationException,
    ILEDelayException,
    ILEException,
    ILESharedConfiguration,
    RandomizableBool,
    RandomizableString,
    find_bounds,
    return_list
)
from .feature_creation_service import (
    BaseFeatureConfig,
    BaseObjectCreationService,
    FeatureCreationService,
    FeatureTypes,
    log_feature_template,
    position_relative_to,
    validate_all_locations_and_update_bounds,
    validate_floor_position
)
from .global_settings_component import ROOM_MIN_XZ, ROOM_MIN_Y
from .interactable_object_service import (
    InteractableObjectConfig,
    InteractableObjectCreationService
)
from .numerics import (
    MinMaxFloat,
    MinMaxInt,
    RandomizableFloat,
    RandomizableInt,
    RandomizableVectorFloat3d,
    RandomizableVectorFloat3dOrFloat,
    VectorFloatConfig,
    VectorIntConfig,
    retrieve_all_vectors
)
from .object_services import (
    DEBUG_FINAL_POSITION_KEY,
    InstanceDefinitionLocationTuple,
    KeywordLocation,
    KeywordLocationConfig,
    ObjectDefinition,
    ObjectRepository,
    RelativePositionConfig,
    add_random_placement_tag,
    calculate_rotated_position,
    get_step_after_movement,
    get_step_after_movement_or_start
)

logger = logging.getLogger(__name__)

# Magic numbers used to create ranges for size, location, scale etc for objects
PLATFORM_SCALE_MIN = 0.5
PLATFORM_SCALE_MAX = 3
PLATFORM_WITH_LIPS_SCALE_MIN = 0.8
RAMP_WIDTH_PERCENT_MIN = 0.05
RAMP_WIDTH_PERCENT_MAX = 0.5
RAMP_LENGTH_PERCENT_MIN = 0.05
RAMP_LENGTH_PERCENT_MAX = 1
WALL_WIDTH_PERCENT_MIN = 0.05
WALL_WIDTH_PERCENT_MAX = 0.5
RAMP_ANGLE_MIN = 15
RAMP_ANGLE_MAX = 45
L_OCCLUDER_SCALE_MIN = 0.5
L_OCCLUDER_SCALE_MAX = 2
DROPPER_THROWER_BUFFER = 0.2
# Ranges for Occluders when values are not specified
DEFAULT_MOVING_OCCLUDER_HEIGHT_MIN = 1
DEFAULT_MOVING_OCCLUDER_HEIGHT_MAX = occluders.OCCLUDER_HEIGHT_TALL
DEFAULT_MOVING_OCCLUDER_WIDTH_MIN = occluders.OCCLUDER_MIN_SCALE_X
DEFAULT_MOVING_OCCLUDER_WIDTH_MAX = 3
DEFAULT_MOVING_OCCLUDER_THICKNESS_MIN = occluders.OCCLUDER_THICKNESS
DEFAULT_MOVING_OCCLUDER_THICKNESS_MAX = occluders.OCCLUDER_THICKNESS
DEFAULT_MOVING_OCCLUDER_REPEAT_MIN = 1
DEFAULT_MOVING_OCCLUDER_REPEAT_MAX = 20
DEFAULT_OCCLUDER_ROTATION_MIN = 0
DEFAULT_OCCLUDER_ROTATION_MAX = 359
DEFAULT_OCCLUDING_WALL_HEIGHT = MinMaxFloat(1, 2)
DEFAULT_OCCLUDING_WALL_THICKNESS = MinMaxFloat(0.1, 0.5)
DEFAULT_OCCLUDING_WALL_WIDTH = MinMaxFloat(1, 2)

# multiply target's scale by these values to make
# sure target is sufficiently occluded.
DEFAULT_OCCLUDING_WALL_HEIGHT_MULTIPLIER = 3.0
DEFAULT_OCCLUDING_WALL_WIDTH_MULTIPLIER = 2.0

# Agents can wall over 0.1 height walls but not 0.15
DEFAULT_OCCLUDING_WALL_SHORT_SCALE_Y_MIN = 0.15

DEFAULT_OCCLUDING_WALL_SHORT_THIN_SCALE_MIN = 0.2
DEFAULT_OCCLUDING_WALL_SHORT_THIN_SCALE_MAX = 0.5

# Height of 0.9 blocks agent, but doesn't look like it should visually
OCCLUDING_WALL_HOLE_MAX_HEIGHT = 0.8
OCCLUDING_WALL_WIDTH_BUFFER = 0.6

ATTACHED_RAMP_MIN_WIDTH = .5
ATTACHED_RAMP_MAX_WIDTH = 1.5
ATTACHED_RAMP_MAX_LENGTH = 3
ATTACHED_RAMP_MIN_LENGTH = .5
TOP_PLATFORM_POSITION_MIN = 0.3

DEFAULT_TURNTABLE_MIN_RADIUS = 0.5
DEFAULT_TURNTABLE_MAX_RADIUS = 1.5

DEFAULT_TURNTABLE_HEIGHT = 0.1

# These are arbitrary but they need to be high enough that a ramp can get up
# the top platform without being greater than 45 degress.
BOTTOM_PLATFORM_SCALE_BUFFER_MIN = geometry.PERFORMER_WIDTH
BOTTOM_PLATFORM_SCALE_BUFFER_MAX = 5

# Use an arbitrarily large number and let the bounds checking determine if
# locations are valid later. We can't determine exactly how much space to use
# when the floor isn't rotated the same.
DEFAULT_AVAILABLE_LENGTHS = (10, 10, 10, 10)
RAMP_ROTATIONS = (90, 180, -90, 0)

WALL_SIDES = ['left', 'right', 'front', 'back', 'back_left_corner',
              'back_right_corner', 'front_left_corner', 'front_right_corner']

# Labels for platforms/ramps
LABEL_PLATFORM = "platforms"
LABEL_RAMP = "ramps"
LABEL_BIDIRECTIONAL_RAMP = "bidirectional"
LABEL_CONNECTED_TO_RAMP = "connected_to_ramp"


def _check_for_collisions(
    scene: Scene,
    definition: ObjectDefinition,
    positions: RandomizableVectorFloat3d,
    rotations: RandomizableVectorFloat3d,
    bounds_list: List[ObjectBounds]
) -> List[Tuple[Vector3d, Vector3d]]:
    """Checks all possible combinations of the given positions and rotations
    for collisions with the given bounds list using the given object defintion,
    and returns the list of all valid combinations as (position, rotation)
    tuples. If either positions or rotations is null/empty, substitutes the
    vector (0, 0, 0). If both positions and rotations are null/empty, returns
    no valid locations."""
    if not positions and not rotations:
        return []
    valid_locations = []
    all_positions = retrieve_all_vectors(
        positions or [VectorFloatConfig(0, 0, 0)]
    )
    all_rotations = retrieve_all_vectors(
        rotations or [VectorIntConfig(0, 0, 0)]
    )
    for position in all_positions:
        position.y = position.y + definition.positionY
        for rotation in all_rotations:
            bounds = geometry.create_bounds(
                vars(definition.dimensions),
                vars(definition.offset),
                vars(position),
                vars(rotation),
                definition.positionY
            )
            if geometry.validate_location_rect(
                bounds,
                vars(scene.performer_start.position),
                bounds_list,
                vars(scene.room_dimensions)
            ):
                valid_locations.append((position, rotation))
    return valid_locations


def _retrieve_object_height_at_step(
    scene: Scene,
    instance: SceneObject,
    step: int
) -> float:
    """Returns the height of the given object at the given step, including the
    object's separate lid, if it has one, and is attached by that step."""
    height = instance['debug']['dimensions']['y']
    lid_id = instance['debug'].get('lidId')
    if lid_id:
        lid = scene.get_object_by_id(lid_id)
        lid_data = lid['lidAttachment']
        if lid_data['stepBegin'] <= step:
            height += lid['debug']['dimensions']['y']
    return height


def _retrieve_scaled_shapes_from_datasets(
    shapes_to_scales: Dict[str, List[Vector3d]]
) -> Dict[str, List[Union[MinMaxFloat, VectorFloatConfig]]]:
    """Return a dict mapping each shape in the given data to each scale
    option, maintaining its aspect ratio, to use as possible default scales."""
    output = {}
    for shape, scales in shapes_to_scales.items():
        if shape not in output:
            output[shape] = []
        output[shape].extend([
            VectorFloatConfig(x=scale.x, y=scale.y, z=scale.z)
            for scale in scales
        ])
    # Override soccer ball scales with specific ILE limitations.
    if 'soccer_ball' in output:
        # Use a single MinMaxFloat to ensure the ball has equal X/Y/Z scales.
        output['soccer_ball'] = [MinMaxFloat(
            min=SOCCER_BALL_SCALE_MIN,
            max=SOCCER_BALL_SCALE_MAX
        )]
    # Remove duplicate scales from the output.
    for shape in output.keys():
        unique_output = []
        for scale in output[shape]:
            if scale not in unique_output:
                unique_output.append(scale)
        output[shape] = unique_output
    # Due to the way the inputs were made, the height for each cylinder shape
    # must be upscaled here, because it will be downscaled again when its
    # ObjectDefinition is created.
    for scale in output.get('cylinder', []):
        scale.y *= 2
    return output


DROPPER_SHAPES_TO_SCALES = _retrieve_scaled_shapes_from_datasets({
    **specific_objects.ROLLABLE_TYPES_TO_SIZES,
    **intuitive_physics_objects.FALL_DOWN_TYPES_TO_SIZES,
    **gravity_support_objects.TYPES_TO_SIZES
})
DROPPER_SHAPES = sorted(list(DROPPER_SHAPES_TO_SCALES.keys()))

PLACER_SHAPES_TO_SCALES = _retrieve_scaled_shapes_from_datasets({
    **specific_objects.ROLLABLE_TYPES_TO_SIZES,
    **specific_objects.CONTAINER_TYPES_TO_SIZES,
    **intuitive_physics_objects.FALL_DOWN_TYPES_TO_SIZES,
    **gravity_support_objects.TYPES_TO_SIZES
})
PLACER_SHAPES = sorted(list(PLACER_SHAPES_TO_SCALES.keys()))

THROWER_SHAPES_TO_SCALES = _retrieve_scaled_shapes_from_datasets({
    **specific_objects.ROLLABLE_TYPES_TO_SIZES,
    **intuitive_physics_objects.MOVE_ACROSS_TYPES_TO_SIZES
})
THROWER_SHAPES = sorted(list(THROWER_SHAPES_TO_SCALES.keys()))


class PassivePhysicsSetup(str, Enum):
    RANDOM = "random"
    ROLL_ANGLED = "roll_angled"
    ROLL_STRAIGHT = "roll_straight"
    TOSS_STRAIGHT = "toss_straight"


class WallSide(str, Enum):
    LEFT = "left"
    RIGHT = "right"
    FRONT = "front"
    BACK = "back"


class OccluderOrigin(str, Enum):
    LEFT = "left"
    RIGHT = "right"
    FRONT = "front"
    BACK = "back"
    TOP = "top"


class OccludingWallType(str, Enum):
    OCCLUDES = "occludes"
    THIN = "thin"
    SHORT = "short"
    HOLE = "hole"


class StructuralWallCreationService(
        BaseObjectCreationService):

    def __init__(self):
        self._default_template = DEFAULT_TEMPLATE_WALL
        self._material_tuple = None
        self._type = FeatureTypes.WALLS

    def create_feature_from_specific_values(
            self, scene: Scene, reconciled: StructuralWallConfig,
            source_template: StructuralWallConfig):
        """Creates a wall from the given template with
        specific values."""
        if not self._material_tuple:
            self._material_tuple = choose_material_tuple_from_material(
                reconciled.material
            )
        args = {
            'position_x': reconciled.position.x,
            'position_y_modifier': reconciled.position.y,
            'position_z': reconciled.position.z,
            'rotation_y': reconciled.rotation_y,
            'material_tuple': self._material_tuple,
            'width': reconciled.width,
            'thickness': reconciled.thickness,
            'height': reconciled.height
        }

        logger.trace(f'Creating interior wall:\nINPUT = {args}')
        new_obj = structures.create_interior_wall(**args)
        new_obj['debug']['ignoreBounds'] = reconciled.ignore_bounds
        new_obj = _post_instance(
            scene, new_obj, reconciled, source_template, self._get_type())
        return new_obj

    def _handle_dependent_defaults(
            self, scene: Scene, reconciled: StructuralWallConfig,
            source_template
    ) -> StructuralWallConfig:
        reconciled = _handle_position_defaults(scene, reconciled)
        configured_material = source_template.material
        if reconciled.same_material_as_room:
            configured_material = scene.room_materials.back
        self._material_tuple = _reconcile_material(
            configured_material,
            materials.ROOM_WALL_MATERIALS
        )
        reconciled.material = self._material_tuple.material
        _, max_room_dim = _get_room_min_max_dimensions(scene)
        reconciled.rotation_y = (
            random.choice([0, 90, 180, 270])
            if reconciled.rotation_y is None else
            reconciled.rotation_y
        )
        reconciled.position = choose_position(
            reconciled.position,
            room_x=scene.room_dimensions.x,
            room_y=scene.room_dimensions.y,
            room_z=scene.room_dimensions.z,
            not_platform=True
        )
        reconciled.width = (MinMaxFloat(
            max_room_dim * WALL_WIDTH_PERCENT_MIN,
            max_room_dim * WALL_WIDTH_PERCENT_MAX).convert_value()
            if reconciled.width is None else reconciled.width)
        reconciled.height = reconciled.height or (
            scene.room_dimensions.y or
            geometry.DEFAULT_ROOM_DIMENSIONS['y']
        )
        return reconciled

    def is_valid(self, scene, new_obj, bounds, try_num, retries):
        valid = (not is_wall_too_close(new_obj[0]))
        return valid and super().is_valid(
            scene,
            new_obj,
            [] if new_obj[0].get('debug', {}).get('ignoreBounds') else bounds,
            try_num,
            retries
        )


class StructuralPlatformCreationService(
        BaseObjectCreationService):

    def __init__(self):
        self._default_template = DEFAULT_TEMPLATE_PLATFORM
        self._material_tuple = None
        self._type = FeatureTypes.PLATFORMS

    def create_feature_from_specific_values(
            self, scene: Scene, reconciled: StructuralPlatformConfig,
            source_template: StructuralPlatformConfig):
        """Creates a platform from the given template with
        specific values."""
        # position
        pos_x = reconciled.position.x
        pos_z = reconciled.position.z
        use_long_x = random.choice([True, False])
        if reconciled.long_with_two_ramps:
            if reconciled.num > 3 and not reconciled.platform_underneath:
                raise ILEException(
                    "Cannot have more than 3 instances of platforms using "
                    "'long_with_two_ramps' property."
                )
            if reconciled.num > 1 and reconciled.platform_underneath:
                raise ILEException(
                    "Cannot have more than 1 instance of a double level "
                    "platform using 'long_with_two_ramps' and "
                    "'platform_underneath' properties."
                )
            reconciled.rotation_y = 0
            pos_x = 0 if use_long_x else reconciled.position.x
            pos_z = 0 if not use_long_x else reconciled.position.z
        # scale
        x = (
            scene.room_dimensions.x if reconciled.long_with_two_ramps and
            use_long_x else
            reconciled.scale.x if isinstance(
                reconciled.scale,
                (Vector3d, VectorFloatConfig)) else reconciled.scale)
        y = (
            reconciled.scale.y if isinstance(
                reconciled.scale,
                (Vector3d, VectorFloatConfig)) else reconciled.scale)
        z = (
            scene.room_dimensions.z if reconciled.long_with_two_ramps and not
            use_long_x else
            reconciled.scale.z if isinstance(
                reconciled.scale,
                (Vector3d, VectorFloatConfig)) else reconciled.scale)
        if not self._material_tuple:
            self._material_tuple = choose_material_tuple_from_material(
                reconciled.material
            )

        # Need to calculate a unique position based on the scale
        # if using adjacent_to_wall
        adj = reconciled.adjacent_to_wall
        if adj:
            if isinstance(adj, list):
                adj = adj[0]
            pos_x, pos_z = _calculate_adjacent_to_wall_pos(
                scene.room_dimensions.x, scene.room_dimensions.z,
                adj, pos_x, pos_z, x, z)
            # These rotations optimize ramp placement for stacked platforms
            # so we don't generate unecessary scenes that will fail.
            # Not really sure why these rotations work though
            if adj == 'left' or adj == 'right':
                reconciled.rotation_y = 0
            if adj == 'back_right_corner' or adj == 'front_left_corner':
                reconciled.rotation_y = 90
            if adj == 'front' or adj == 'back':
                reconciled.rotation_y = 180
            if adj == 'back_left_corner' or adj == 'front_right_corner':
                reconciled.rotation_y = 270
        args = {
            'position_x': pos_x,
            'position_y_modifier': reconciled.position.y,
            'position_z': pos_z,
            'rotation_y': reconciled.rotation_y,
            'material_tuple': self._material_tuple,
            'lips': (reconciled.lips if not reconciled.long_with_two_ramps else
                     ALL_LIPS),
            'scale_x': x,
            'scale_y': y,
            'scale_z': z,
            'room_dimension_y': scene.room_dimensions.y,
            'auto_adjust_platform': reconciled.auto_adjust_platforms
        }

        logger.trace(f'Creating platform:\nINPUT = {args}')
        new_obj = [structures.create_platform(**args)]
        new_obj = _post_instance(
            scene, new_obj, reconciled, source_template, self._get_type())
        if adj:
            new_obj[0]['debug']['adjacent_to_wall'] = adj
        return new_obj

    def _handle_dependent_defaults(
            self, scene: Scene, reconciled: StructuralPlatformConfig,
            source_template
    ) -> StructuralPlatformConfig:
        reconciled = _handle_position_defaults(scene, reconciled)
        self._material_tuple = _reconcile_material(
            source_template.material,
            materials.ROOM_WALL_MATERIALS
        )
        reconciled.material = self._material_tuple.material
        room_height = (
            scene.room_dimensions.y or geometry.DEFAULT_ROOM_DIMENSIONS['y'])
        # Wall height always equals room height.

        scale = Vector3d()
        if isinstance(reconciled.scale, Vector3d):
            scale.x = reconciled.scale.x
            # Restrict max height to room height.
            scale.y = min(reconciled.scale.y, room_height)
            scale.z = reconciled.scale.z
        else:
            scale.x = reconciled.scale
            # Restrict max height to room height.
            scale.y = min(reconciled.scale, room_height)
            scale.z = reconciled.scale
        if reconciled.lips and (reconciled.lips.right or reconciled.lips.left):
            scale.x = max(scale.x, PLATFORM_WITH_LIPS_SCALE_MIN)
        if reconciled.lips and (reconciled.lips.front or reconciled.lips.back):
            scale.z = max(scale.z, PLATFORM_WITH_LIPS_SCALE_MIN)
        reconciled.scale = scale
        return reconciled


class StructuralRampCreationService(
        BaseObjectCreationService):

    def __init__(self):
        self._default_template = DEFAULT_TEMPLATE_RAMP
        self._material_tuple = None
        self._type = FeatureTypes.RAMPS

    def create_feature_from_specific_values(
            self, scene: Scene, reconciled: StructuralRampConfig,
            source_template: StructuralRampConfig):
        """Creates a ramp from the given template with
        specific values."""
        if not self._material_tuple:
            self._material_tuple = choose_material_tuple_from_material(
                reconciled.material
            )
        args = {
            'position_x': reconciled.position.x,
            'position_y_modifier': reconciled.position.y,
            'position_z': reconciled.position.z,
            'rotation_y': reconciled.rotation_y,
            'material_tuple': self._material_tuple,
            'angle': reconciled.angle,
            'width': reconciled.width,
            'length': reconciled.length
        }

        logger.trace(f'Creating ramp:\nINPUT = {args}')
        new_obj = structures.create_ramp(**args)
        new_obj = _post_instance(
            scene, new_obj, reconciled, source_template, self._get_type())
        return new_obj

    def _handle_dependent_defaults(
            self, scene: Scene, reconciled: StructuralRampConfig,
            source_template
    ) -> StructuralRampConfig:
        reconciled = _handle_position_defaults(scene, reconciled)
        self._material_tuple = _reconcile_material(
            source_template.material,
            materials.ROOM_WALL_MATERIALS
        )
        reconciled.material = self._material_tuple.material
        room_height = (
            scene.room_dimensions.y or geometry.DEFAULT_ROOM_DIMENSIONS['y'])
        min_room_dim, _ = _get_room_min_max_dimensions(scene)

        max_length = (
            (room_height - geometry.PERFORMER_HEIGHT) /
            math.tan(math.radians(reconciled.angle))
        )
        reconciled.width = getattr(reconciled, 'width', None) or MinMaxFloat(
            min_room_dim * RAMP_WIDTH_PERCENT_MIN,
            min_room_dim * RAMP_WIDTH_PERCENT_MAX
        ).convert_value()
        reconciled.length = getattr(reconciled, 'length', None) or MinMaxFloat(
            min_room_dim * RAMP_LENGTH_PERCENT_MIN,
            min(min_room_dim * RAMP_LENGTH_PERCENT_MAX, max_length)
        ).convert_value()
        return reconciled


class StructuralLOccluderCreationService(
        BaseObjectCreationService):

    def __init__(self):
        self._default_template = DEFAULT_TEMPLATE_L_OCCLUDER
        self._material_tuple = None
        self._type = FeatureTypes.L_OCCLUDERS

    def create_feature_from_specific_values(
            self, scene: Scene, reconciled: StructuralLOccluderConfig,
            source_template: StructuralLOccluderConfig):
        """Attempts to adds an L occluder from the given template with
        specific values."""
        if not self._material_tuple:
            self._material_tuple = choose_material_tuple_from_material(
                reconciled.material
            )
        args = {
            'flip': reconciled.backwards,
            'position_x': reconciled.position.x,
            'position_y_modifier': reconciled.position.y,
            'position_z': reconciled.position.z,
            'rotation_y': reconciled.rotation_y,
            'material_tuple': self._material_tuple,
            'scale_front_x': reconciled.scale_front_x,
            'scale_front_z': reconciled.scale_front_z,
            'scale_side_x': reconciled.scale_side_x,
            'scale_side_z': reconciled.scale_side_z,
            'scale_y': reconciled.scale_y
        }

        logger.trace(f'Creating l occluder:\nINPUT = {args}')
        new_obj = structures.create_l_occluder(
            **args)
        new_obj = _post_instance(
            scene, new_obj, reconciled, source_template, self._get_type())
        return new_obj

    def _handle_dependent_defaults(
            self, scene: Scene, reconciled: StructuralLOccluderConfig,
            source_template
    ) -> StructuralLOccluderConfig:
        reconciled = _handle_position_defaults(scene, reconciled)
        self._material_tuple = _reconcile_material(
            source_template.material,
            materials.ROOM_WALL_MATERIALS
        )
        reconciled.material = self._material_tuple.material
        room_height = (
            scene.room_dimensions.y or geometry.DEFAULT_ROOM_DIMENSIONS['y'])
        reconciled.scale_y = min(reconciled.scale_y, room_height)
        return reconciled


class StructuralDropperCreationService(
        BaseObjectCreationService):
    bounds = []
    dropper = None

    def __init__(self):
        self._default_template = DEFAULT_TEMPLATE_DROPPER
        self._type = FeatureTypes.DROPPERS

    def create_feature_from_specific_values(
            self, scene: Scene, reconciled: StructuralDropperConfig,
            source_template: StructuralDropperConfig):
        """Creates a dropper from the given template with
        specific values."""
        room_dim = scene.room_dimensions

        args = {}
        if (reconciled.no_projectile):
            self.target = None
            self.target_exists = False
            dummy_dimensions = Vector3d(x=random.uniform(0.5, 1.0),
                                        y=random.uniform(0.5, 1.0),
                                        z=random.uniform(0.5, 1.0)
                                        )
            projectile_dimensions = vars(dummy_dimensions)
            args = {
                'position_x': reconciled.position_x,
                'position_z': reconciled.position_z,
                'room_dimensions_y': room_dim.y,
                'object_dimensions': projectile_dimensions,
                'last_step': scene.goal.last_step,
                'dropping_step': reconciled.drop_step,
            }
        else:
            self.target, self.target_exists = _get_projectile_idl(
                reconciled,
                scene,
                self.bounds or [],
                DROPPER_SHAPES_TO_SCALES
            )
            projectile_dimensions = vars(self.target.definition.dimensions)
            args = {
                'position_x': reconciled.position_x,
                'position_z': reconciled.position_z,
                'room_dimensions_y': room_dim.y,
                'object_dimensions': projectile_dimensions,
                'last_step': scene.goal.last_step,
                'dropping_step': reconciled.drop_step,
                'is_round': ('ball' in self.target.definition.shape)
            }

        logger.trace(f'Creating dropper:\nINPUT = {args}')
        new_obj = [mechanisms.create_dropping_device(**args)]
        self.dropper = new_obj[0]
        if not reconciled.no_projectile:
            if not self.target_exists:
                new_obj.append(self.target.instance)

        # In passive physics scenes, change the dropper's position to just
        # outside the camera's viewport (instead of attached to the ceiling).
        if scene.intuitive_physics:
            position_y = retrieve_off_screen_position_y(reconciled.position_z)
            self.dropper['shows'][0]['position']['y'] = round(position_y, 4)

        add_random_placement_tag(new_obj, source_template)
        return new_obj

    def _handle_dependent_defaults(
            self, scene: Scene, reconciled: StructuralDropperConfig,
            source_template
    ) -> StructuralDropperConfig:
        buffer = DROPPER_THROWER_BUFFER
        room_dim = scene.room_dimensions
        reconciled.position_x = (
            random.uniform(-room_dim.x /
                           2.0 + buffer, room_dim.x / 2.0 - buffer)
            if reconciled.position_x is None else reconciled.position_x)
        reconciled.position_z = (
            random.uniform(-room_dim.z /
                           2.0 + buffer, room_dim.z / 2.0 - buffer)
            if reconciled.position_z is None else reconciled.position_z)

        # If needed, adjust this device's position relative to another object.
        if source_template.position_relative:
            position_x, position_z = position_relative_to(
                # Use the config list from the source template.
                source_template.position_relative,
                (reconciled.position_x, reconciled.position_z),
                scene.performer_start.position,
                'dropping device'
            )
            if position_x is not None:
                reconciled.position_x = position_x
            if position_z is not None:
                reconciled.position_z = position_z

        # Save the projectile labels from the source template.
        self._target_labels = source_template.projectile_labels

        if isinstance(reconciled.projectile_dimensions, (int, float)):
            val = reconciled.projectile_dimensions
            reconciled.projectile_dimensions = Vector3d(x=val, y=val, z=val)

        return reconciled

    def _on_valid_instances(self, scene, reconciled_template, new_obj):
        super()._on_valid_instances(scene, reconciled_template, new_obj)
        self._do_post_add(scene, reconciled_template)

    def _do_post_add(self, scene, reconciled):
        target = None
        args = {}
        if self.target is None:
            args = {
                'dropping_device': self.dropper,
                'dropping_step': reconciled.drop_step
            }
        else:
            target = self.target
            args = {
                'instance': target.instance,
                'dropping_device': self.dropper,
                'dropping_step': reconciled.drop_step
            }

        logger.trace(f'Positioning dropper object:\nINPUT = {args}')
        if target is not None:
            mechanisms.drop_object(**args)
            target.instance['debug']['positionedBy'] = 'mechanism'
            target.instance['debug'][DEBUG_FINAL_POSITION_KEY] = True

        # Override other properties for a passive physics scene (if needed).
        if scene.intuitive_physics and target is not None:
            # Only show the target on the step that it's dropped.
            target.instance['shows'][0]['stepBegin'] = reconciled.drop_step
            # Don't show the dropper at all.
            self.dropper['shows'][0]['stepBegin'] = -1

        if not self.target_exists and target is not None:
            log_feature_template('dropper object', 'id', target.instance['id'])
        else:
            if target is not None:
                for i in range(len(scene.objects)):
                    if scene.objects[i]['id'] == target.instance['id']:
                        scene.objects[i] = target.instance

        object_repo = ObjectRepository.get_instance()
        if target is not None:
            object_repo.add_to_labeled_objects(target, self._target_labels)


class StructuralThrowerCreationService(
        BaseObjectCreationService):
    bounds = []
    thrower = None

    def __init__(self):
        self._default_template = DEFAULT_TEMPLATE_THROWER
        self._path_relative_object = None
        self._type = FeatureTypes.THROWERS

    def create_feature_from_specific_values(
            self, scene: Scene, reconciled: StructuralThrowerConfig,
            source_template: StructuralThrowerConfig):
        """Creates a thrower from the given template with
        specific values."""
        room_dim = scene.room_dimensions
        # Don't want to validate against the walls since the throwers
        # intentionally intersect with walls.
        room_dimensions_extended = Vector3d(
            x=room_dim.x + 2,
            y=room_dim.y,
            z=room_dim.z + 2
        )
        scene_copy = copy.deepcopy(scene)
        scene_copy.room_dimensions = room_dimensions_extended
        projectile_dimensions = None

        if reconciled.no_projectile:
            self.target = None
            self.target_exists = False
            dummy_dimensions = Vector3d(x=random.uniform(0.5, 1.0),
                                        y=random.uniform(0.5, 1.0),
                                        z=random.uniform(0.5, 1.0)
                                        )
            projectile_dimensions = vars(dummy_dimensions)

        else:
            self.target, self.target_exists = _get_projectile_idl(
                reconciled,
                scene_copy,
                self.bounds or [],
                THROWER_SHAPES_TO_SCALES
            )
            projectile_dimensions = vars(self.target.definition.dimensions)

        wall_rot = {
            WallSide.LEFT.value: 0,
            WallSide.RIGHT: 180,
            WallSide.FRONT: 90,
            WallSide.BACK: 270
        }

        max_scale = max(projectile_dimensions['x'], projectile_dimensions['z'])

        # If passive_physics_setup is set, then override all default and
        # configured options with values used in passive physics scenes.
        if reconciled.passive_physics_setup:
            reconciled.wall = random.choice([WallSide.LEFT, WallSide.RIGHT])
            reconciled.rotation_z = 0
            choices = [reconciled.passive_physics_setup]
            if reconciled.passive_physics_setup == PassivePhysicsSetup.RANDOM:
                choices = [
                    PassivePhysicsSetup.ROLL_ANGLED,
                    PassivePhysicsSetup.ROLL_STRAIGHT,
                    PassivePhysicsSetup.TOSS_STRAIGHT
                ]
            choice = random.choice(choices)
            # Normal movement
            if choice == PassivePhysicsSetup.ROLL_STRAIGHT:
                reconciled.height = 0
                reconciled.rotation_y = 0
                reconciled.position_wall = random.uniform(
                    MIN_TARGET_Z,
                    MAX_TARGET_Z_STRAIGHT_ACROSS
                )
            # Tossed movement
            if choice == PassivePhysicsSetup.TOSS_STRAIGHT:
                reconciled.height = 1
                reconciled.rotation_y = 0
                reconciled.position_wall = random.uniform(
                    MIN_TARGET_Z,
                    MAX_TARGET_Z_STRAIGHT_ACROSS
                )
            # Angled movement
            if choice == PassivePhysicsSetup.ROLL_ANGLED:
                reconciled.height = 0
                rotation_choices = [-20, 20]
                if reconciled.wall == WallSide.LEFT:
                    rotation_choices = [20, -20]
                reconciled.rotation_y = random.choice(rotation_choices)
                # Back-to-front
                if reconciled.rotation_y == rotation_choices[0]:
                    reconciled.position_wall = MAX_TARGET_Z
                # Front-to-back
                if reconciled.rotation_y == rotation_choices[1]:
                    reconciled.position_wall = MIN_TARGET_Z

        if reconciled.wall in [WallSide.FRONT, WallSide.BACK]:
            pos_x = reconciled.position_wall
            pos_z = ((room_dim.z - max_scale) / 2.0) * (
                1 if reconciled.wall == WallSide.FRONT else -1
            )
        if reconciled.wall in [WallSide.LEFT, WallSide.RIGHT]:
            pos_z = reconciled.position_wall
            pos_x = ((room_dim.x - max_scale) / 2.0) * (
                -1 if reconciled.wall == WallSide.LEFT else 1
            )
            # In passive physics scenes, change the thrower's position to just
            # outside the camera's viewport (instead of attached to the wall).
            if scene.intuitive_physics:
                pos_x = round(retrieve_off_screen_position_x(pos_z), 4)
                pos_x *= (-1 if reconciled.wall == WallSide.LEFT else 1)

        rotation_y = wall_rot[reconciled.wall] + reconciled.rotation_y
        if self._path_relative_object:
            if reconciled.path_relative.option == 'collide':
                pointing_at_vector = Vector3d(
                    x=self._path_relative_object['shows'][0]['position']['x'],
                    y=self._path_relative_object['shows'][0]['position']['y'],
                    z=self._path_relative_object['shows'][0]['position']['z']
                )
                _, rotation_y = geometry.calculate_rotations(
                    Vector3d(x=pos_x, y=reconciled.height, z=pos_z),
                    pointing_at_vector,
                    no_rounding_to_tens=True
                )
                # Convert the performer agent rotation into thrower rotation.
                rotation_y = (rotation_y - 90) % 360
                # Throwers should not have Y rotation outside 90 deg arc.
                if abs(rotation_y - wall_rot[reconciled.wall]) > 45:
                    raise ILEException(
                        f'Throwing device on {reconciled.wall} wall with '
                        f'"path_relative" set to collide with '
                        f'{reconciled.path_relative.labels} must '
                        f'have calculated Y rotation between '
                        f'{wall_rot[reconciled.wall] - 45} and '
                        f'{wall_rot[reconciled.wall] + 45} but was '
                        f'{rotation_y}'
                    )
            elif reconciled.path_relative.option == 'avoid':
                # Create a line the length of the room.
                path = shapely.geometry.LineString([
                    [0, 0], [0, max(room_dim.x, room_dim.z)]
                ])
                # Ensure the line is the width of the thrown object.
                target_width = self.target.instance['debug']['dimensions']['x']
                path = path.buffer(target_width / 2.0)
                # Retrieve all possible rotations for the throwing device.
                rotation_y_list = return_list(source_template.rotation_y)
                if not rotation_y_list:
                    # Default between -45 and 45 degrees.
                    rotation_y_list = list(range(-45, 50, 5))
                # Randomize them.
                random.shuffle(rotation_y_list)

                bounds = self._path_relative_object['shows'][0]['boundingBox']
                successful = False
                # Loop over all the possible Y rotation values...
                for rotation_y_option in rotation_y_list:
                    # Override the existing rotation_y here.
                    rotation_y = (
                        wall_rot[reconciled.wall] + rotation_y_option
                    ) % 360
                    # Rotate and translate the line appropriately.
                    rotated_path = shapely.affinity.rotate(
                        path,
                        # Add 90 here to convert the performer agent rotation
                        # into thrower rotation.
                        -(rotation_y + 90),
                        origin=(0, 0)
                    )
                    rotated_path = shapely.affinity.translate(
                        rotated_path,
                        pos_x,
                        pos_z
                    )
                    # If the line does not intersect with the relative object,
                    # then this Y rotation is valid.
                    if not rotated_path.intersects(bounds.polygon_xz):
                        successful = True
                        break
                # If all possible rotations are invalid, then raise an error.
                if not successful:
                    raise ILEException(
                        f'Cannot assign a Y rotation for a throwing device to '
                        f'ensure a thrown object will avoid object '
                        f'{self._path_relative_object["id"]}'
                    )

        args = {}

        if (reconciled.no_projectile):
            args = {
                'position_x': pos_x,
                'position_y': reconciled.height,
                'position_z': pos_z,
                'rotation_y': rotation_y,
                'rotation_z': reconciled.rotation_z,
                'object_dimensions': projectile_dimensions,
                'last_step': scene.goal.last_step,
                'throwing_step': reconciled.throw_step,
            }

        else:
            args = {
                'position_x': pos_x,
                'position_y': reconciled.height,
                'position_z': pos_z,
                'rotation_y': rotation_y,
                'rotation_z': reconciled.rotation_z,
                'object_dimensions': projectile_dimensions,
                'object_rotation_y': self.target.definition.rotation.y,
                'last_step': scene.goal.last_step,
                'throwing_step': reconciled.throw_step,
                'is_round': ('ball' in self.target.definition.shape)
            }

        logger.trace(f'Creating thrower:\nINPUT = {args}')
        new_obj = [mechanisms.create_throwing_device(**args)]
        self.thrower = new_obj[0]
        if not self.target_exists and not reconciled.no_projectile:
            new_obj.append(self.target.instance)

        add_random_placement_tag(new_obj, source_template)
        return new_obj

    def _handle_dependent_defaults(
            self, scene: Scene, reconciled: StructuralThrowerConfig,
            source_template
    ) -> StructuralThrowerConfig:
        room_dim = scene.room_dimensions
        buffer = DROPPER_THROWER_BUFFER
        on_side_wall = reconciled.wall in [WallSide.LEFT, WallSide.RIGHT]
        if reconciled.position_wall is None:
            if not on_side_wall:
                reconciled.position_wall = random.uniform(
                    -room_dim.x / 2.0 + buffer, room_dim.x /
                    2.0 - buffer)
            else:
                reconciled.position_wall = random.uniform(
                    -room_dim.z /
                    2.0 + buffer, room_dim.z /
                    2.0 - buffer)

        if reconciled.height is None:
            reconciled.height = random.uniform(
                buffer, room_dim.y - buffer)

        # If needed, adjust this device's position relative to another object.
        if source_template.position_relative:
            position_x, position_z = position_relative_to(
                # Use the config list from the source template.
                source_template.position_relative,
                (
                    reconciled.position_wall if not on_side_wall else 0,
                    reconciled.position_wall if on_side_wall else 0
                ),
                scene.performer_start.position,
                'throwing device'
            )
            if not on_side_wall and position_x is not None:
                reconciled.position_wall = position_x
            if on_side_wall and position_z is not None:
                reconciled.position_wall = position_z

        self._path_relative_object = None
        object_repo = ObjectRepository.get_instance()
        avoid = reconciled.path_relative.option == 'avoid'
        collide = reconciled.path_relative.option == 'collide'
        if avoid or collide:
            for label in return_list(reconciled.path_relative.labels):
                idl = object_repo.get_one_from_labeled_objects(label)
                if idl:
                    self._path_relative_object = idl.instance
                    break
            if not idl:
                raise ILEDelayException(
                    f'Cannot find object with one of the following labels for '
                    f'thrown object to {"collide" if collide else "avoid"}: '
                    f'{reconciled.path_relative.labels}'
                )

        # Save the projectile labels from the source template.
        self._target_labels = source_template.projectile_labels

        if isinstance(reconciled.projectile_dimensions, (int, float)):
            val = reconciled.projectile_dimensions
            reconciled.projectile_dimensions = Vector3d(x=val, y=val, z=val)

        return reconciled

    def is_valid(self, scene, new_obj, bounds, try_num, retries):
        # droppers are intentionally embedded in walls.
        altered_scene: Scene = copy.deepcopy(scene)
        altered_scene.room_dimensions.x += 2
        altered_scene.room_dimensions.z += 2
        # Throwers should ignore the holes and lava directly underneath them.
        bounds = find_bounds(scene, ignore_ground=True)
        return super().is_valid(
            altered_scene, new_obj, bounds, try_num, retries)

    def _on_valid_instances(self, scene, reconciled_template, new_obj):
        super()._on_valid_instances(scene, reconciled_template, new_obj)
        self._do_post_add(scene, reconciled_template)

    def __use_stop_position_config(self, scene, reconciled) -> float:
        if reconciled.height not in [0, 1]:
            raise ILEConfigurationException(
                f'The ILE does not have "stop_position" movement data for the '
                f'configured thrower height ({reconciled.height}). Please '
                f'use one of the following thrower heights: 0, 1'
            )

        thrower_x = round(self.thrower['shows'][0]['position']['x'], 4)
        thrower_y = round(self.thrower['shows'][0]['position']['y'], 4)
        thrower_z = round(self.thrower['shows'][0]['position']['z'], 4)
        thrower_angle = self.thrower['shows'][0]['rotation']['y']

        is_left_to_right = (thrower_x < 0)
        if thrower_angle in [0, 180]:
            is_straight = True
            is_back_to_front = False
            throw_angle = 0
        elif is_left_to_right:
            is_straight = False
            is_back_to_front = (thrower_angle > 0)
            throw_angle = thrower_angle - 0
        else:
            is_straight = False
            is_back_to_front = (thrower_angle < 180)
            throw_angle = thrower_angle - 180

        # Base case: use X/Z config
        stop_x = reconciled.stop_position.x
        stop_z = reconciled.stop_position.z

        # If "offscreen" is set, calculate the offscreen X/Z position.
        if scene.intuitive_physics and reconciled.stop_position.offscreen:
            if is_straight:
                stop_x = retrieve_off_screen_position_x(thrower_z)
                stop_x = stop_x * (1 if is_left_to_right else -1)
                stop_z = thrower_z
            else:
                if is_back_to_front:
                    func = find_off_screen_position_diagonal_toward
                    # This shouldn't happen if passive_physics_setup is set.
                    on_error = (
                        "Try moving the thrower further back, away from the " +
                        "performer agent's starting position."
                    )
                else:
                    func = find_off_screen_position_diagonal_away
                    # This shouldn't happen if passive_physics_setup is set.
                    on_error = (
                        "Try moving the thrower closer to the performer " +
                        "agent's starting position, and/or decrease its " +
                        "angle to 20 or less."
                    )
                stop_x, stop_z = func(thrower_x, thrower_z, abs(throw_angle))
                if not stop_x or not stop_z:
                    raise ILEException(
                        f'A thrower configured with a "stop_position" of '
                        f'"offscreen" ({thrower_x=}, {thrower_y=}, '
                        f'{thrower_z=}, {throw_angle=}) cannot find a valid '
                        f'X/Z stop position ({stop_x=}, {stop_z=}). {on_error}'
                    )

        # If "behind" is set, calculate the X/Z position behind the occluder.
        if scene.intuitive_physics and reconciled.stop_position.behind:
            object_repo = ObjectRepository.get_instance()
            label = reconciled.stop_position.behind
            if not object_repo.has_label(label):
                raise ILEDelayException(
                    f'Cannot find object label {label} to identify thrower '
                    f'"stop_position" for "behind" configuration.'
                )
            idl = object_repo.get_one_from_labeled_objects(label)
            occluder_x = idl.instance['shows'][0]['position']['x']
            occluder_z = idl.instance['shows'][0]['position']['z']
            if is_back_to_front:
                func = find_position_behind_occluder_diagonal_toward
            else:
                func = find_position_behind_occluder_diagonal_away
            stop_x, stop_z = func(
                occluder_x,
                occluder_z,
                thrower_x,
                thrower_z,
                abs(throw_angle)
            )
            logger.debug(
                f'Found stop position behind occluder {label}: '
                f'{stop_x=}, {stop_z=}'
            )
            if not stop_x or not stop_z:
                raise ILEException(
                    f'A thrower configured with a "stop_position" of '
                    f'"behind" ({thrower_x=}, {thrower_y=}, {thrower_z=}, '
                    f'{throw_angle=}, {occluder_x=}, {occluder_z=}) cannot '
                    f'find a valid X/Z stop position ({stop_x=}, {stop_z=}). '
                    f'Try adjusting the position or angle of the thrower, '
                    f'and/or the position of occluder {label}.'
                )

        # Validation for the X/Z if neither "offscreen" nor "behind".
        if stop_x is None or stop_z is None:
            raise ILEConfigurationException(
                f'A thrower configured with "stop_position" must have both X '
                f'and Z, but at least one was null: {stop_x=} {stop_z=}'
            )

        # Find the required distance the object must travel.
        required_distance = math.dist([thrower_x, thrower_z], [stop_x, stop_z])
        required_distance = round(required_distance, 2)

        # Ensure the throw angle is valid. The X/Z values from "offscreen" or
        # "behind" (in passive physics scenes) should always be valid.
        x_distance = round(thrower_x - stop_x, 2)
        required_angle = 90 - math.degrees(math.asin(
            math.radians(x_distance) / math.radians(required_distance)
        )) - 180
        if not math.isclose(abs(required_angle), abs(throw_angle), abs_tol=5):
            raise ILEException(
                f'A thrower configured with "stop_position" ({thrower_x=}, '
                f'{thrower_y=}, {thrower_z=}, {thrower_angle=}, {stop_x=}, '
                f'{stop_z=}) does not have a valid angle; throw angle must be '
                f'{abs(required_angle)} but was {abs(throw_angle)}'
            )

        # Retrieve all the available movement data, which records how far an
        # object is expected to travel under a specific force.
        moves = TOSS_MOVE_LIST if reconciled.height == 1 else BASE_MOVE_LIST
        valid_moves = []
        all_distances = []
        for move in moves:
            # Compare the distance the object will travel before it stops
            # (using the current movement) to the required distance.
            distance_by_step = move['xDistanceByStep']
            move_distance = distance_by_step[-1] - distance_by_step[0]
            all_distances.append(round(move_distance, 1))
            # For offscreen stop positions, the required distance is simply a
            # minimum distance (the object can roll as far as it wants to!).
            if scene.intuitive_physics and reconciled.stop_position.offscreen:
                if (move_distance + 0.1) >= required_distance:
                    valid_moves.append(move)
            else:
                if math.isclose(
                    move_distance,
                    required_distance,
                    rel_tol=0.1,
                    abs_tol=0.1
                ):
                    valid_moves.append(move)

        # We don't have every possible move distance pre-recorded (just what is
        # needed in passive physics scenes), since that would take up a lot of
        # development time, so just raise an error in most cases.
        if not valid_moves:
            # In passive physics scenes, where the thrown object starts off-
            # screen, if we need the object to stop behind an occluder, try
            # moving the object back a little bit, so the required distance
            # matches one of our available distances, and the object will still
            # be offscreen anyway.
            if scene.intuitive_physics and reconciled.stop_position.behind:
                hypotenuse = min([
                    round(distance - required_distance, 4)
                    for distance in all_distances
                ])
                angle_1 = abs(throw_angle)
                angle_2 = 90 - angle_1
                distance_z = hypotenuse * math.sin(math.radians(angle_1))
                distance_x = hypotenuse * math.sin(math.radians(angle_2))
                distance_x *= (-1 if is_left_to_right else 1)
                thrower_position = self.thrower['shows'][0]['position']
                thrower_position['x'] += distance_x
                thrower_position['z'] += distance_z
                return self.__use_stop_position_config(scene, reconciled)
            move_type = 'tossed' if reconciled.height == 1 else 'non-tossed'
            raise ILEConfigurationException(
                f'The ILE does not have "stop_position" movement data for the '
                f'required throw distance ({thrower_x=}, {thrower_y=}, '
                f'{thrower_z=}, {throw_angle=}, {stop_x=}, {stop_z=}, '
                f'{required_distance=}). Please ensure the total distance is '
                f'approximately equal to one of the following {move_type} '
                f'movement values: {sorted(set(all_distances))}'
            )

        # These forces are all non-impulse.
        reconciled.impulse = False

        # Choose a random force from the valid movements.
        chosen_move = random.choice(valid_moves)

        # Some movements have a Y force, in addition to the X force.
        return chosen_move['forceX'], chosen_move.get('forceY', 0)

    def __use_throw_force_config(self, scene, reconciled) -> float:
        force_x = reconciled.throw_force
        force_y = 0

        # Apply the throw_force_multiplier (if any) based on the room size.
        if reconciled.throw_force_multiplier:
            force_x = reconciled.throw_force_multiplier
            room_size = scene.room_dimensions
            on_side_wall = reconciled.wall in [WallSide.LEFT, WallSide.RIGHT]
            force_x *= room_size.x if on_side_wall else room_size.z

        # Use the correct default throw force (if needed).
        elif force_x is None:
            force_x = choose_random(
                DEFAULT_THROW_FORCE_IMPULSE if reconciled.impulse else
                DEFAULT_THROW_FORCE_NON_IMPULSE
            )

        # Override the throw_force for a passive physics scene (if needed).
        if reconciled.passive_physics_throw_force:
            forces = NON_COLLISION_SPEEDS
            if reconciled.rotation_z:
                forces = NON_COLLISION_ANGLED_SPEEDS
            force_x = random.choice(forces)
            # Add a Y force for "toss" movement if the object is in the air.
            if reconciled.height == 1:
                force_y = random.choice(TOSS_SPEEDS)
            reconciled.impulse = False
        if reconciled.passive_physics_collision_force:
            forces = COLLISION_SPEEDS
            force_x = random.choice(forces)
            reconciled.impulse = False

        return force_x, force_y

    def _do_post_add(self, scene, reconciled):
        target = self.target
        if (target is not None):
            if reconciled.stop_position:
                try:
                    force_x, force_y = self.__use_stop_position_config(
                        scene,
                        reconciled
                    )
                except Exception as exception:
                    # If something goes wrong, ensure the corresponding objects
                    # do not remain in the scene (they will be remade).
                    scene.objects = [
                        instance for instance in scene.objects
                        if instance['id'] not in
                        [target.instance['id'], self.thrower['id']]
                    ]
                    raise exception from exception
            else:
                force_x, force_y = self.__use_throw_force_config(
                    scene, reconciled)

            # Override other properties for passive physics
            # scene (if needed).
            if scene.intuitive_physics:
                # Only show the target on the step that it's thrown.
                target.instance['shows'][0]['stepBegin'] = \
                    reconciled.throw_step
                # Don't show the thrower at all.
                self.thrower['shows'][0]['stepBegin'] = -1

            # Make sure we multiply the force by the target's mass!
            force_x *= target.definition.mass
            force_y *= target.definition.mass

            # Update the target to be thrown.
            args = {
                'instance': target.instance,
                'throwing_device': self.thrower,
                'throwing_force': force_x,
                'throwing_step': reconciled.throw_step,
                'impulse': reconciled.impulse
            }
            logger.trace(f'Positioning thrower object:\nINPUT = {args}')
            target.instance['debug']['positionedBy'] = 'mechanism'
            target.instance['debug'][DEBUG_FINAL_POSITION_KEY] = True
            mechanisms.throw_object(**args)

            # Update the thrown object's Y force if needed.
            target.instance['forces'][0]['vector']['y'] = force_y

            # If the thrown object should start on the ground, make sure it
            # isn't centered in the thrower instead.
            if reconciled.height == 0:
                starting_y = target.definition.positionY
                target.instance['shows'][0]['position']['y'] = starting_y

            # Make sure the target exists in the scene.
            if not self.target_exists:
                log_feature_template(
                    'thrower object', 'id', target.instance['id'])
            else:
                for i in range(len(scene.objects)):
                    if scene.objects[i]['id'] == target.instance['id']:
                        scene.objects[i] = target.instance

            object_repo = ObjectRepository.get_instance()
            object_repo.add_to_labeled_objects(target, self._target_labels)

        else:
            return


class StructuralMovingOccluderCreationService(
        BaseObjectCreationService):

    def __init__(self):
        self._default_template = DEFAULT_TEMPLATE_MOVING_OCCLUDER
        self._type = FeatureTypes.MOVING_OCCLUDERS
        self._pole_material_tuple = None
        self._wall_material_tuple = None

    def create_feature_from_specific_values(
            self, scene: Scene, reconciled: StructuralMovingOccluderConfig,
            source_template: StructuralMovingOccluderConfig):
        """Creates a moving occluder from the given template with
        specific values."""
        room_dim = scene.room_dimensions
        last_step_arg = scene.goal.last_step if (
            not reconciled.repeat_movement and
            reconciled.move_up_before_last_step
        ) else None
        repeat_movement_arg = (
            None if (reconciled.move_down_only or
                     not reconciled.repeat_movement) else
            reconciled.repeat_interval
        )
        if not self._pole_material_tuple:
            self._pole_material_tuple = choose_material_tuple_from_material(
                reconciled.pole_material
            )
        if not self._wall_material_tuple:
            self._wall_material_tuple = choose_material_tuple_from_material(
                reconciled.wall_material
            )
        args = {
            'wall_material': self._wall_material_tuple,
            'pole_material': self._pole_material_tuple,
            'x_position': reconciled.position_x,
            'occluder_width': reconciled.occluder_width,
            'occluder_height': reconciled.occluder_height,
            'occluder_thickness': reconciled.occluder_thickness,
            'last_step': last_step_arg,
            'repeat_movement': repeat_movement_arg,
            'reverse_direction': reconciled.reverse_direction,
            'room_dimensions': vars(room_dim),
            'sideways_back': reconciled.origin == OccluderOrigin.BACK,
            'sideways_front': reconciled.origin == OccluderOrigin.FRONT,
            'sideways_left': reconciled.origin == OccluderOrigin.LEFT,
            'sideways_right': reconciled.origin == OccluderOrigin.RIGHT,
            'y_rotation': reconciled.rotation_y,
            'z_position': reconciled.position_z,
            'move_down_only': reconciled.move_down_only
        }

        logger.trace(f'Creating moving occluder:\nINPUT = {args}')
        new_obj = occluders.create_occluder(**args)

        shared_config = ILESharedConfiguration.get_instance()
        if scene.intuitive_physics:
            scene, new_obj = occluder_gap_positioning(
                scene,
                shared_config.get_occluder_gap(),
                shared_config.get_occluder_gap_viewport(),
                new_obj,
                reconciled.origin in ['right', 'left'])

        new_obj = _post_instance(
            scene, new_obj, reconciled, source_template, self._get_type())
        return new_obj

    def _handle_dependent_defaults(
            self, scene: Scene, reconciled: StructuralMovingOccluderConfig,
            source_template
    ) -> StructuralMovingOccluderConfig:
        room_dim = scene.room_dimensions
        max_size = max(
            reconciled.occluder_thickness,
            reconciled.occluder_width)
        limit_x = room_dim.x - max_size
        limit_z = room_dim.z - max_size
        limit_x /= 2.0
        limit_z /= 2.0
        reconciled.position_x = (
            reconciled.position_x if reconciled.position_x is not None
            else random.uniform(-limit_x, limit_x))
        reconciled.position_z = (
            reconciled.position_z if reconciled.position_z is not None
            else random.uniform(-limit_z, limit_z))
        reconciled.occluder_height = min(
            reconciled.occluder_height, room_dim.y)
        self._wall_material_tuple = _reconcile_material(
            source_template.wall_material,
            materials.ROOM_WALL_MATERIALS
        )
        reconciled.wall_material = self._wall_material_tuple.material
        self._pole_material_tuple = _reconcile_material(
            source_template.pole_material,
            materials.METAL_MATERIALS
        )
        reconciled.pole_material = self._pole_material_tuple.material
        return reconciled

    def _on_valid_instances(self, scene, reconciled_template, new_obj):
        super()._on_valid_instances(scene, reconciled_template, new_obj)
        # Remove the template's labels from the occluder's pole; we only ever
        # want the configured labels to reference the occluder's wall.
        ObjectRepository.get_instance().remove_from_labeled_objects(
            new_obj[1]['id'],
            reconciled_template.labels
        )


class StructuralPartitionFloorCreationService(BaseObjectCreationService):
    def __init__(self):
        self._default_template = DEFAULT_TEMPLATE_PARTITION_FLOOR
        self._type = FeatureTypes.PARTITION_FLOOR

    def create_feature_from_specific_values(
            self, scene: Scene, reconciled: PartitionFloorConfig,
            source_template: PartitionFloorConfig):
        return PartitionFloor(reconciled.leftHalf,
                              reconciled.rightHalf)

    def is_valid(self, scene: Scene, partitions: List,
                 bounds, try_num, retries):
        if partitions:
            part_bounds = geometry.find_partition_floor_bounds(
                scene.room_dimensions, partitions[0])
            for bb in part_bounds:
                if not geometry.validate_location_rect(
                    bb,
                    vars(scene.performer_start.position),
                    bounds,
                    vars(scene.room_dimensions)
                ):
                    return False
        return True

    def _on_valid_instances(
            self, scene: Scene, reconciled_template, partitions):
        if partitions:
            scene.partition_floor = partitions[0]
            log_feature_template(
                'partition_floor', 'partition_floor',
                partitions[0], [reconciled_template])


class StructuralLavaCreationService(
        BaseObjectCreationService):

    def __init__(self):
        self._default_template = DEFAULT_TEMPLATE_HOLES_LAVA
        self._type = FeatureTypes.LAVA

    def create_feature_from_specific_values(
            self, scene: Scene, reconciled: FloorAreaConfig,
            source_template: FloorAreaConfig):
        """Creates lava from the given template with
        specific values."""
        return _create_contiguous_floor_features(
            scene,
            reconciled.position_x,
            reconciled.position_z,
            reconciled.size,
            self.bounds,
            'lava'
        )

    def _handle_dependent_defaults(
            self, scene: Scene, reconciled: FloorAreaConfig, source_template
    ) -> FloorAreaConfig:
        _add_floor_dependent_defaults(scene, reconciled)
        return reconciled

    def is_valid(
        self,
        scene: Scene,
        position_list: List[Vector2dInt],
        bounds_list: List[ObjectBounds],
        try_num: int,
        retries: int
    ) -> bool:
        return _is_valid_floor(
            scene,
            position_list,
            'lava',
            True,
            bounds_list,
            try_num,
            retries,
            self._get_type()
        )

    def _on_valid_instances(self, scene, reconciled_template, new_obj):
        scene.lava += new_obj
        log_feature_template(
            'lava', 'lava', new_obj, [reconciled_template])


class StructuralHolesCreationService(
        BaseObjectCreationService):

    def __init__(self):
        self._default_template = DEFAULT_TEMPLATE_HOLES_LAVA
        self._type = FeatureTypes.HOLES

    def create_feature_from_specific_values(
            self, scene: Scene, reconciled: FloorAreaConfig,
            source_template: FloorAreaConfig):
        """Creates a hole from the given template with
        specific values."""
        return _create_contiguous_floor_features(
            scene,
            reconciled.position_x,
            reconciled.position_z,
            reconciled.size,
            self.bounds,
            'holes'
        )

    def _handle_dependent_defaults(
            self, scene: Scene, reconciled: FloorAreaConfig, source_template
    ) -> FloorAreaConfig:
        _add_floor_dependent_defaults(scene, reconciled)
        return reconciled

    def is_valid(
        self,
        scene: Scene,
        position_list: List[Vector2dInt],
        bounds_list: List[ObjectBounds],
        try_num: int,
        retries: int
    ) -> bool:
        return _is_valid_floor(
            scene,
            position_list,
            'holes',
            True,
            bounds_list,
            try_num,
            retries,
            self._get_type()
        )

    def _on_valid_instances(
        self,
        scene: Scene,
        reconciled_template,
        new_obj
    ):
        scene.holes += new_obj
        log_feature_template(
            'holes', 'holes', new_obj, [reconciled_template])


class StructuralFloorMaterialsCreationService(
        BaseObjectCreationService):

    def __init__(self):
        self._default_template = DEFAULT_TEMPLATE_FLOOR_MATERIALS
        self._type = FeatureTypes.FLOOR_MATERIALS

    def create_feature_from_specific_values(
            self, scene: Scene, reconciled: FloorMaterialConfig,
            source_template: FloorMaterialConfig):
        """Returns the floor material template.  This creation service
        uses the specific template itself to add to scene instead of
        creating it first."""
        return reconciled

    def _handle_dependent_defaults(
            self, scene: Scene, reconciled: FloorMaterialConfig,
            source_template
    ) -> FloorMaterialConfig:
        _add_floor_dependent_defaults(scene, reconciled)
        reconciled.material = _reconcile_material(
            source_template.material if source_template else None,
            materials.FLOOR_MATERIALS + materials.ROOM_WALL_MATERIALS
        ).material
        return reconciled

    def is_valid(self, scene: Scene, objs: List, bounds, try_num, retries):
        template: FloorMaterialConfig = objs[0]
        room_dim = scene.room_dimensions
        x = template.position_x
        z = template.position_z
        xmax = math.floor(room_dim.x / 2)
        zmax = math.floor(room_dim.z / 2)
        valid = not (x < -xmax or x > xmax or z < -zmax or z > zmax)
        if not valid:
            return False

        # make sure there is no lava here
        lava = scene.lava or []
        pos = Vector2dInt(x=template.position_x, z=template.position_z)
        if pos in lava:
            return False
        for existing in scene.floor_textures:
            for existing_pos in existing.positions:
                if (existing_pos.x == template.position_x and
                        existing_pos.z == template.position_z):
                    return False
        return True

    def _on_valid_instances(
            self, scene: Scene, reconciled: FloorMaterialConfig,
            new_obj):
        mat = reconciled.material
        pos = Vector2dInt(x=reconciled.position_x, z=reconciled.position_z)
        added = False
        for existing in scene.floor_textures:
            if existing.material == mat:
                existing.positions.append(pos)
                added = True
        if not added:
            scene.floor_textures.append(FloorTexturesConfig(
                material=mat,
                positions=[pos]
            ))
        log_feature_template(
            'floor_materials', 'floor_materials', new_obj, [reconciled])


class StructuralOccludingWallsCreationService(
        BaseObjectCreationService):

    def __init__(self):
        self._default_template = DEFAULT_TEMPLATE_OCCLUDING_WALL
        self._type = FeatureTypes.OCCLUDING_WALLS

    def create_feature_from_specific_values(
            self, scene: Scene, reconciled: StructuralOccludingWallConfig,
            source_template: StructuralOccludingWallConfig):
        """Not currently implemented.  This creation service overrides
        add_to_scene itself for now."""
        room_dim = scene.room_dimensions
        defn = _get_occluding_wall_definition(reconciled)
        if (reconciled.keyword_location is not None):
            # setup keyword location
            idl = KeywordLocation.get_keyword_location_object_tuple(
                reconciled.keyword_location,
                source_template.keyword_location,
                defn,
                scene.performer_start,
                self.bounds,
                room_dim
            )
            idl.instance['id'] = f"{reconciled.type}-{idl.instance['id']}"
            result = _modify_for_hole(
                reconciled.type, idl.instance, self.target_dim)
        else:
            # setup x, z location
            defn.scale = reconciled.scale

            location = {
                'position': {
                    'x': reconciled.position.x,
                    'y': reconciled.scale.y / 2.0,
                    'z': reconciled.position.z
                },
                'rotation': {
                    'x': 0,
                    'y': reconciled.rotation_y,
                    'z': 0
                }
            }
            inst = instances.instantiate_object(defn, location)
            inst = structures.finalize_structural_object([inst])[0]
            inst['id'] = f"{reconciled.type}-{inst['id']}"
            result = _modify_for_hole(reconciled.type, inst, self.target_dim)
        add_random_placement_tag(result, source_template)
        return result

    def _handle_dependent_defaults(
            self, scene: Scene, reconciled: StructuralOccludingWallConfig,
            source_template: StructuralOccludingWallConfig
    ) -> StructuralOccludingWallConfig:
        obj_repo = ObjectRepository.get_instance()
        reconciled.material = _reconcile_material(
            source_template.material,
            materials.ROOM_WALL_MATERIALS
        ).material
        if (
            reconciled.keyword_location is None and
            reconciled.position.x is None and
            reconciled.position.y is None and
            reconciled.position.z is None and
            any([(not idl.instance['debug'].get('positionedBy')) for idl in (
                obj_repo.get_all_from_labeled_objects(TARGET_LABEL)
                if obj_repo.has_label(TARGET_LABEL) else []
            )])
        ):
            reconciled.keyword_location = KeywordLocationConfig(keyword=(
                'between' if reconciled.type == OccludingWallType.THIN else
                'occlude'
            ), relative_object_label=TARGET_LABEL)

        # For thin occluding walls, replace an 'occlude' keyword_location with
        # 'between' because using 'occlude' doesn't make sense for thin walls!
        # This also helps configuring randomly generating occluding walls.
        if (
            reconciled.keyword_location and
            reconciled.keyword_location.keyword == 'occlude' and
            reconciled.type == OccludingWallType.THIN
        ):
            reconciled.keyword_location.keyword = 'between'

        self.target = None
        if (
            reconciled.keyword_location and
            reconciled.keyword_location.relative_object_label
        ):
            label = reconciled.keyword_location.relative_object_label
            self.target = obj_repo.get_one_from_labeled_objects(label)
            if self.target:
                reconciled.keyword_location.relative_object_label = (
                    self.target.instance['id'])

        reconciled.scale, self.target_dim = _get_occluding_wall_scale(
            reconciled, self.target, scene.room_dimensions.y)

        scale = reconciled.scale
        room_dim = scene.room_dimensions
        max_size = math.sqrt(math.pow(scale.x, 2) +
                             math.pow(scale.z, 2))
        limit_x = room_dim.x - max_size
        limit_z = room_dim.z - max_size

        reconciled.position.x = (
            MinMaxFloat(-limit_x, limit_x).convert_value()
            if reconciled.position.x is None else reconciled.position.x
        )
        reconciled.position.z = (
            MinMaxFloat(-limit_z, limit_z).convert_value()
            if reconciled.position.z is None else reconciled.position.z
        )

        return reconciled


class StructuralPlacersCreationService(
        BaseObjectCreationService):
    object_idl = None
    bounds = []

    def __init__(self):
        self._default_template = DEFAULT_TEMPLATE_PLACER
        self._type = FeatureTypes.PLACERS

    def create_feature_from_specific_values(
            self, scene: Scene, reconciled: StructuralPlacerConfig,
            source_template: StructuralPlacerConfig):
        """Creates a placer from the given template with
        specific values."""
        try:
            return self._create_feature_from_specific_values_helper(
                scene,
                reconciled,
                source_template
            )
        except Exception as e:
            self._cleanup_on_failure()
            raise e from e

    def reconcile(
        self,
        scene: Scene,
        source_template: StructuralPlacerConfig
    ) -> StructuralPlacerConfig:
        try:
            return super().reconcile(scene, source_template)
        except Exception as e:
            self._cleanup_on_failure()
            raise e from e

    def _cleanup_on_failure(self) -> None:
        if self.object_idl:
            placed_object = self.object_idl.instance
            placed_object['debug']['positionedBy'] = None
            placed_object['debug'][DEBUG_FINAL_POSITION_KEY] = None

    def _create_feature_from_specific_values_helper(
            self, scene: Scene, reconciled: StructuralPlacerConfig,
            source_template: StructuralPlacerConfig):
        """Creates a placer from the given template with
        specific values."""

        room_dim = scene.room_dimensions
        start_height = reconciled.placed_object_position.y
        max_height = room_dim.y
        last_step = scene.goal.last_step

        # The object_idl is None if empty_placer is True
        idl = self.object_idl
        if idl:
            geometry.move_to_location(idl.instance, {
                'position': vars(reconciled.placed_object_position),
                'rotation': vars(
                    VectorIntConfig(0, reconciled.placed_object_rotation, 0)
                )
            })

            idl.instance['debug']['positionedBy'] = 'mechanism'
            idl.instance['debug'][DEBUG_FINAL_POSITION_KEY] = True

            if (start_height - idl.instance['debug']['positionY']) <= 0:
                if reconciled.pickup_object or reconciled.move_object:
                    start_height = 0
                else:
                    start_height = room_dim.y

        args_place = {
            'instance': idl.instance if idl else None,
            'activation_step': reconciled.activation_step,
            'start_height': start_height,
            'end_height': reconciled.end_height,
            'deactivation_step': reconciled.deactivation_step
        }

        args_pickup = {
            'instance': idl.instance if idl else None,
            'activation_step': reconciled.activation_step,
            'start_height': start_height,
            'room_height': max_height,
            'deactivation_step': reconciled.deactivation_step
        }

        args_move = {
            'instance': idl.instance if idl else None,
            'activation_step': reconciled.activation_step,
            'room_height': max_height,
            'move_object_end_position': reconciled.move_object_end_position.x
            if reconciled.move_object_end_position else None,
            'move_object_y': reconciled.move_object_y,
            'move_object_z': reconciled.move_object_z,
        }

        if idl and reconciled.pickup_object:
            logger.trace(
                f'Positioning placer to pickup object:\nINPUT = {args_pickup}'
            )
            mechanisms.pickup_object(**args_pickup)
        elif idl and reconciled.move_object:
            logger.trace(
                f'Positioning placer to move object:\nINPUT = {args_move}'
            )
            # Add the movement to the object.
            mechanisms.move_object(**args_move)
            new_x = reconciled.move_object_end_position.x
            new_z = idl.instance['shows'][0]['position']['z']
            idl.instance['debug']['moveToPosition'] = {
                'x': new_x,
                'y': idl.instance['shows'][0]['position']['y'],
                'z': new_z
            }
            idl.instance['debug']['moveToPositionBy'] = (
                idl.instance['moves'][-1]['stepEnd']
            )

            objects_to_reposition = []

            # If our object is a container with a separate lid, and the lid is
            # attached after our container is moved, reposition the lid/placer.
            lid_id = idl.instance['debug'].get('lidId')
            lid = scene.get_object_by_id(lid_id)
            lid_placer_id = idl.instance['debug'].get('lidPlacerId')
            lid_placer = scene.get_object_by_id(lid_placer_id)
            if lid and lid_placer:
                object_move_begin = reconciled.activation_step
                object_move_end = idl.instance['moves'][-1]['stepEnd']
                lid_placer_move_begin = lid_placer['moves'][0]['stepBegin']
                lid_placer_move_end = lid_placer['moves'][-1]['stepEnd']
                if (
                    lid_placer_move_begin <= object_move_end and
                    lid_placer_move_end >= object_move_begin
                ):
                    raise ILEException(
                        f'Placer is configured to move a container with a '
                        f'separate lid ({idl.instance["id"]}) between steps '
                        f'{object_move_begin} and {object_move_end}, but that '
                        f'would overlap with the placer attaching the lid '
                        f'between steps {lid_placer_move_begin} and '
                        f'{lid_placer_move_end}. Please adjust this placer\'s '
                        f'"activation_step", or the container\'s '
                        f'"separate_lid" configuration.'
                    )
                if lid_placer_move_begin > object_move_end:
                    objects_to_reposition.extend([lid, lid_placer])

            # If any other objects were positioned above our moved object,
            # and are "placed" after our object was moved, reposition them.
            for possibly_held in scene.objects:
                # Ignore our moved object!
                if possibly_held['id'] == idl.instance['id']:
                    continue
                # If this object was positioned by a placer...
                if possibly_held['debug'].get('positionedBy') != 'mechanism':
                    continue
                # If this object was positioned above our moved object...
                above_id = possibly_held['debug'].get('positionedAboveId')
                if above_id != idl.instance['id']:
                    continue
                # Find its corresponding placer...
                for possible_placer in scene.objects:
                    # If this object is a placer holding the other object...
                    held_id = possible_placer['debug'].get('heldObjectId')
                    if held_id != possibly_held['id']:
                        continue
                    # If this placer activates after our object was moved...
                    move_step = possible_placer['moves'][0]['stepBegin']
                    if move_step > reconciled.activation_step:
                        # Reposition both the placer and its held object.
                        objects_to_reposition.append([
                            possibly_held,
                            possible_placer
                        ])

            for object_to_reposition in objects_to_reposition:
                object_to_reposition['shows'][0]['position']['x'] = new_x
                object_to_reposition['shows'][0]['position']['z'] = new_z
        elif idl:
            logger.trace(
                f'Positioning placer to place object:\nINPUT = {args_place}'
            )
            mechanisms.place_object(**args_place)

        objs = []
        if idl and idl.instance not in scene.objects:
            objs.append(idl.instance)

        # Create single placers when empty_placer is true
        placer_offset_x = idl.definition.placerOffsetX if idl else [0]
        placer_offset_y = idl.definition.placerOffsetY if idl else [0]
        placer_offset_z = idl.definition.placerOffsetZ if idl else [0]

        placer_offset_list = placer_offset_x
        use_x_offset = True
        if not placer_offset_list or placer_offset_list == [0]:
            # The separate_container has a Z placer offset rather than an X.
            placer_offset_list = placer_offset_z
            use_x_offset = False

        idl_position = (
            idl.instance['shows'][0]['position'] if idl else
            vars(reconciled.placed_object_position)
        )
        idl_dimensions = idl.instance['debug']['dimensions'] if idl else None
        idl_standing_y = idl.instance['debug']['positionY'] if idl else 0

        for index, placer_offset in enumerate(placer_offset_list or [0]):
            # Adjust the placer offset based on the object's Y rotation.
            offset_line = shapely.geometry.LineString([
                [0, 0],
                [placer_offset, 0] if use_x_offset else [0, placer_offset]
            ])
            offset_line = shapely.affinity.rotate(
                offset_line,
                -reconciled.placed_object_rotation,
                origin=(0, 0)
            )
            resolved_offset_x, resolved_offset_z = list(offset_line.coords)[1]
            # Adjust the placer position based on the offsets.
            position = copy.deepcopy(idl_position)
            position['x'] += resolved_offset_x
            position['z'] += resolved_offset_z
            # Create the new placer and add it to the scene.
            args = {
                'placed_object_position': position,
                # If empty_placer is True, use 0 for each dimension.
                'placed_object_dimensions': {
                    'x': idl_dimensions['x'] if idl else 0,
                    'y': _retrieve_object_height_at_step(
                        scene,
                        idl.instance,
                        reconciled.activation_step
                    ) if idl else 0,
                    'z': idl_dimensions['z'] if idl else 0
                },
                'placed_object_offset_y': idl_standing_y,
                'activation_step': reconciled.activation_step,
                'end_height': reconciled.end_height,
                'max_height': max_height,
                'id_modifier': None,
                'last_step': last_step,
                'placed_object_placer_offset_y': placer_offset_y[index],
                'deactivation_step': reconciled.deactivation_step,
                'is_pickup_obj': reconciled.pickup_object,
                'is_move_obj': reconciled.move_object,
                'move_object_end_position':
                reconciled.move_object_end_position.x
                if reconciled.move_object_end_position else None,
                'move_object_y': reconciled.move_object_y,
                'move_object_z': reconciled.move_object_z,
            }
            logger.trace(f'Creating placer:\nINPUT = {args}')
            placer = mechanisms.create_placer(**args)
            placer['debug']['heldObjectId'] = (
                idl.instance['id'] if idl else None
            )
            _post_instance(
                scene,
                placer,
                reconciled,
                source_template,
                self._get_type())
            objs.append(placer)

        return objs

    def _restriction_validation(
            self, template, scene):

        if template is not None:
            if template.empty_placer and (
                template.placed_object_labels or
                template.placed_object_material or
                template.placed_object_rotation or
                template.placed_object_scale or
                template.placed_object_shape
            ):
                raise ILEConfigurationException(
                    "Error with placer "
                    "configuration. When 'empty_placer'=True "
                    "then the following options must NOT be included in "
                    "the configuration: 'placed_object_labels', "
                    "'placed_object_material', 'placed_object_rotation', "
                    "'placed_object_scale', 'placed_object_shape'"
                )
            if template.move_object and \
                    template.move_object_end_position is None:
                raise ILEConfigurationException(
                    "Error with placer "
                    "configuration. When 'move_object'=True "
                    "then move_object_end_position is required in "
                    "the configuration.")
            if template.move_object and template.move_object_y is not None:
                scene_obj_y = []
                combo_y_list = list()
                for obj in scene.objects:
                    scene_obj_y.append(obj['debug']['dimensions']['y'])
                combo_y_list = list(combinations(scene_obj_y, 2))
                for item_y in combo_y_list:
                    total = 0
                    for ele in range(0, len(item_y)):
                        total += (item_y[ele] *
                                  mechanisms.MOVE_OBJ_OFFSET) if ele == 0 \
                            else item_y[ele]
                    if total >= scene.room_dimensions.y:
                        raise ILEConfigurationException(
                            "Error with move_object configuration. The height "
                            "of the objects and ceiling height may not not "
                            "allow enough clearance to move one object over "
                            "another. Adjust the scale of the object or the "
                            "room dimention(y).")
            # TODO: Add collision detection along the object's entire path

    def _handle_dependent_defaults(
            self, scene: Scene, reconciled: StructuralPlacerConfig,
            source_template
    ) -> StructuralPlacerConfig:
        self._restriction_validation(source_template, scene)
        # Make sure to reset the object_idl on each run.
        self.object_idl = None

        obj_repo = ObjectRepository.get_instance()
        room_dim = scene.room_dimensions
        defn = None

        # Save the object labels from the source template.
        self._target_labels = source_template.placed_object_labels
        if self._target_labels:
            self.object_idl = _get_existing_held_object_idl(
                self._target_labels,
                place_or_pickup=(not reconciled.move_object),
                existing_object_required=reconciled.existing_object_required
            )

        if self.object_idl:
            logger.trace(
                f'Using existing object for placer: '
                f'{self.object_idl.instance["id"]}'
            )
        if not self.object_idl and not reconciled.empty_placer:
            # Choose a shape now, so we can set the default scale accordingly.
            shape, material = choose_shape_material(
                reconciled.placed_object_shape,
                reconciled.placed_object_material
            )
            # Use SHAPES_TO_SCALES to choose a default scale for the shape.
            # If the shape isn't in SHAPES_TO_SCALES, then something is wrong,
            # so throw an error.
            scale = (
                reconciled.placed_object_scale or
                PLACER_SHAPES_TO_SCALES[shape]
            )
            # Don't set position or rotation for now; it will be done soon.
            obj_cfg = InteractableObjectConfig(
                num=1,
                scale=scale,
                shape=shape,
                material=(material.material if material else None),
                labels=self._target_labels)

            srv: InteractableObjectCreationService = (
                FeatureCreationService.get_service(FeatureTypes.INTERACTABLE))
            obj_reconciled = srv.reconcile(scene, obj_cfg)
            instance = srv.create_feature_from_specific_values(
                scene, obj_reconciled, obj_cfg)
            defn = getattr(srv, 'defn', None)
            self.object_idl = InstanceDefinitionLocationTuple(
                instance, defn, None)
            logger.trace(
                f'Creating new object for placer: '
                f'{self.object_idl.instance["id"]}'
            )

        if reconciled.activate_after:
            step = get_step_after_movement([
                label for label in return_list(source_template.activate_after)
                if label
            ])
            if step >= 1:
                reconciled.activation_step = step

        if reconciled.activate_on_start_or_after:
            step = get_step_after_movement_or_start([
                label for label in
                return_list(source_template.activate_on_start_or_after)
                if label
            ])
            if step >= 1:
                reconciled.activation_step = step

        if reconciled.placed_object_above:
            # Position the held object above another object in the scene.
            if not obj_repo.has_label(reconciled.placed_object_above):
                raise ILEDelayException(
                    f'Cannot find the configured placed_object_above label '
                    f'"{reconciled.placed_object_above}" to position a new '
                    f'placer.'
                )

            above_object = obj_repo.get_one_from_labeled_objects(
                label=reconciled.placed_object_above
            ).instance
            above_position = above_object['shows'][0]['position'].copy()
            above_debug = above_object['debug']

            # If the object was moved by something like another placer before
            # this placer activates, then use the object's moved position.
            if (
                above_debug.get('moveToPosition') and
                above_debug['moveToPositionBy'] <= reconciled.activation_step
            ):
                above_position['x'] = above_debug['moveToPosition']['x']
                above_position['z'] = above_debug['moveToPosition']['z']

            # If the object was rotated by a turntable before this placer
            # activates, then use the object's rotated position.
            if above_debug.get('isRotatedBy'):
                adjusted_position = calculate_rotated_position(
                    scene,
                    reconciled.activation_step,
                    above_object
                )
                if adjusted_position:
                    above_position['x'] = adjusted_position['x']
                    above_position['z'] = adjusted_position['z']

            reconciled.placed_object_position = Vector3d(
                x=above_position['x'],
                y=above_position['y'],
                z=above_position['z']
            )
            above_id = above_object['id']
            if self.object_idl:
                instance = self.object_idl.instance
                instance['debug']['positionedAboveId'] = above_id

        # Retrieve the bounds for all objects in the scene, but ignore the held
        # object's current bounds, because they will change. Used below.
        bounds_list = find_bounds(scene, ignore_ids=(
            [self.object_idl.instance['id']] if self.object_idl else []
        ))
        # Add the bounds for moved objects to the bounds_list.
        for instance in scene.objects:
            if instance['debug'].get('moveToPosition'):
                position = instance['debug']['moveToPosition'].copy()
                position['y'] = instance['shows'][0]['position']['y']
                bounds_list.append(geometry.create_bounds(
                    instance['debug']['dimensions'],
                    instance['debug']['offset'],
                    position,
                    instance['shows'][0]['rotation'],
                    instance['debug']['positionY']
                ))

        if reconciled.retain_position and not reconciled.empty_placer:
            reconciled.placed_object_position = VectorFloatConfig(
                x=self.object_idl.instance['shows'][0]['position']['x'],
                y=self.object_idl.instance['shows'][0]['position']['y'],
                z=self.object_idl.instance['shows'][0]['position']['z']
            )

        # If this placer picks up or moves its object, check for collisions
        # with the object's starting position (assuming it is configured),
        # since it will begin on the ground.
        if (
            source_template.placed_object_position and
            (reconciled.pickup_object or reconciled.move_object) and
            not reconciled.empty_placer
        ):
            # Convert the configured Y rotations into rotation vectors.
            config_rotations = source_template.placed_object_rotation or []
            if not isinstance(config_rotations, list):
                config_rotations = [config_rotations]
            rotation_vectors = [
                VectorIntConfig(0, rotation_y, 0)
                for rotation_y in config_rotations
            ]
            valid_start_locations = _check_for_collisions(
                scene,
                self.object_idl.definition,
                source_template.placed_object_position,
                rotation_vectors,
                bounds_list
            )
            if not valid_start_locations:
                data = [vars(position) for position in (
                    source_template.placed_object_position if
                    isinstance(source_template.placed_object_position, list)
                    else [source_template.placed_object_position]
                )]
                raise ILEException(
                    f'Placer with configured placed_object_position='
                    f'{data} pickup up or moving object with '
                    f'id={self.object_idl.instance["id"]} would collide '
                    f'with an existing object in the scene.'
                )
            # Randomly choose from one of the valid options.
            position, rotation = random.choice(valid_start_locations)
            reconciled.placed_object_position = position
            reconciled.placed_object_rotation = rotation.y
        else:
            # Otherwise choose position and rotation the normal way.
            reconciled.placed_object_rotation = choose_rotation(
                VectorIntConfig(0, reconciled.placed_object_rotation, 0)).y
            object_idl = self.object_idl if self.object_idl else None
            reconciled.placed_object_position = choose_position(
                reconciled.placed_object_position,
                object_idl.definition.dimensions.x if object_idl else 0,
                object_idl.definition.dimensions.z if object_idl else 0,
                room_dim.x,
                room_dim.y,
                room_dim.z,
                True
            )
            # For normal empty placers, if the Y position was not configured by
            # the user, then adjust it to be near the ceiling, rather than
            # being at floor level (choose_position will return a Y of 0).
            source_position = source_template.placed_object_position
            if (
                reconciled.empty_placer and
                not (reconciled.move_object or reconciled.pickup_object) and
                (source_position is None or source_position.y is None)
            ):
                reconciled.placed_object_position.y = room_dim.y - 0.25

        # If this placer moves its object, check for collisions with the
        # object's ending position.
        if (
            source_template.move_object_end_position and
            not reconciled.empty_placer
        ):
            rotation = self.object_idl.instance['shows'][0]['rotation']
            # Include the object's new starting position.
            start_bounds = geometry.create_bounds(
                self.object_idl.instance['debug']['dimensions'],
                self.object_idl.instance['debug']['offset'],
                vars(reconciled.placed_object_position),
                rotation,
                self.object_idl.instance['debug']['positionY']
            )
            move_object_end_position = copy.deepcopy(return_list(
                source_template.move_object_end_position
            ))
            for position_config in move_object_end_position:
                position_config.y = (
                    reconciled.placed_object_position.y -
                    # Subtract positionY here since _check_for_collisions will
                    # add it again.
                    self.object_idl.definition.positionY
                )
                position_config.z = reconciled.placed_object_position.z
            valid_end_locations = _check_for_collisions(
                scene,
                self.object_idl.definition,
                move_object_end_position,
                VectorIntConfig(rotation['x'], rotation['y'], rotation['z']),
                bounds_list + [start_bounds]
            )
            if not valid_end_locations:
                data = [
                    f'x={position.x}' for position in move_object_end_position
                ]
                raise ILEException(
                    f'Placer with configured move_object_end_position='
                    f'{data} moving object with '
                    f'id={self.object_idl.instance["id"]} and position='
                    f'{reconciled.placed_object_position} would collide '
                    f'with an existing object in the scene.'
                )
            # Randomly choose from one of the valid options.
            position, _ = random.choice(valid_end_locations)
            reconciled.move_object_end_position = position

        # If needed, adjust this placer's position relative to another object.
        if source_template.position_relative:
            position_x, position_z = position_relative_to(
                # Use the config list from the source template.
                source_template.position_relative,
                (
                    reconciled.placed_object_position.x,
                    reconciled.placed_object_position.z
                ),
                scene.performer_start.position,
                'placer'
            )
            if position_x is not None:
                reconciled.placed_object_position.x = position_x
            if position_z is not None:
                reconciled.placed_object_position.z = position_z

        # Set the placer end_height based on a end_height_object_label,
        # if that is specified
        if reconciled.end_height_relative_object_label:
            # This may need to be revisited/incorporated into
            # position_relative_to() if there will be cases where the x/z
            # set above won't match the y position set here, due to multiple
            # objects in a scene with the same relative object label.
            end_height_obj_label = reconciled.end_height_relative_object_label
            if not obj_repo.has_label(end_height_obj_label):
                raise ILEDelayException(
                    f'Cannot find end height relative object label '
                    f'"{end_height_obj_label}" to position a new placer'
                )
            else:
                end_height_obj = obj_repo.get_one_from_labeled_objects(
                    label=end_height_obj_label)
                obj_inst = end_height_obj.instance
                new_end_height = obj_inst['shows'][0]['scale']['y']
                reconciled.end_height = new_end_height

        return reconciled

    def _on_valid_instances(
            self, scene: Scene, reconciled_template: StructuralPlacerConfig,
            new_obj: SceneObject, key: str = 'objects'):

        # The object_idl is None if empty_placer is True
        if self.object_idl:
            self.object_idl.instance['debug']['positionedBy'] = 'mechanism'
            self.object_idl.instance['debug'][DEBUG_FINAL_POSITION_KEY] = True

            object_repo = ObjectRepository.get_instance()
            object_repo.add_to_labeled_objects(
                self.object_idl,
                self._target_labels
            )

        return super()._on_valid_instances(
            scene, reconciled_template, new_obj, key)

    def is_valid(self, scene, new_obj, bounds, try_num, retries) -> bool:
        # Ignore the bounds of all placers, and all objects that are positioned
        # by mechanisms, to support tasks like Shell Game, since the placers
        # and the objects they hold (like the target object or a container lid)
        # will intentionally start very close together.
        bounds_subset = find_bounds(scene, ignore_ids=[
            instance['id'] for instance in scene.objects if (
                instance['id'].startswith('placer') or
                instance['debug'].get('positionedBy') == 'mechanism'
            )
        ])
        return super().is_valid(
            scene,
            new_obj,
            bounds_subset,
            try_num,
            retries
        )


class StructuralDoorsCreationService(
        BaseObjectCreationService):

    def __init__(self):
        self._type = FeatureTypes.DOORS
        self._default_template = DEFAULT_TEMPLATE_DOOR
        self._door_material_tuple = None
        self._wall_material_tuple = None

    def create_feature_from_specific_values(
            self, scene: Scene, reconciled: StructuralDoorConfig,
            source_template: StructuralDoorConfig):
        """Creates a door from the given template with
        specific values."""
        if not self._door_material_tuple:
            self._door_material_tuple = choose_material_tuple_from_material(
                reconciled.material
            )
        if not self._wall_material_tuple:
            self._wall_material_tuple = choose_material_tuple_from_material(
                reconciled.wall_material
            )
        args = {
            'position_x': reconciled.position.x,
            'position_y': reconciled.position.y,
            'position_z': reconciled.position.z,
            'rotation_y': reconciled.rotation_y,
            'material_tuple': self._door_material_tuple,
            'wall_scale_x': reconciled.wall_scale_x,
            'wall_scale_y': reconciled.wall_scale_y,
            'wall_material_tuple': self._wall_material_tuple

        }
        logger.trace(f'Creating door:\nINPUT = {args}')
        door_objs = structures.create_door(**args)
        door = door_objs[0]
        _post_instance(
            scene,
            door,
            reconciled,
            source_template,
            self._get_type())
        add_random_placement_tag(door_objs, source_template)
        return door_objs

    def _handle_dependent_defaults(
            self, scene: Scene, reconciled: StructuralDoorConfig,
            source_template
    ) -> StructuralDoorConfig:
        room_dim = scene.room_dimensions
        def_dim = geometry.DEFAULT_ROOM_DIMENSIONS
        room_width = room_dim.x or def_dim['x']
        room_length = room_dim.z or def_dim['z']

        # Wall and Door materials should not be the same unless
        # both are configured to be the same
        door_materials = materials.METAL_MATERIALS + \
            materials.PLASTIC_MATERIALS + materials.WOOD_MATERIALS
        prohibited_wall_material = None
        # If wall_material is provided in the source template
        if source_template.wall_material is not None:
            # Reconcile the wall material
            self._wall_material_tuple = _reconcile_material(
                source_template.wall_material, materials.ROOM_WALL_MATERIALS)
            reconciled.wall_material = self._wall_material_tuple.material
            # Prohibit using the same material for the door
            prohibited_wall_material = reconciled.wall_material

        # Reconcile the door material while avoiding the
        # prohibited_wall_material which may be an str or None
        self._door_material_tuple = _reconcile_material(
            source_template.material, door_materials,
            prohibited_material=prohibited_wall_material)
        reconciled.material = self._door_material_tuple.material

        # If wall_material is not provided in the source template
        if source_template.wall_material is None:
            # Reconcile the wall material while avoiding the door material
            self._wall_material_tuple = _reconcile_material(
                source_template.wall_material, materials.ROOM_WALL_MATERIALS,
                prohibited_material=reconciled.material)
            reconciled.wall_material = self._wall_material_tuple.material

        reconciled.position.x = choose_random(
            MinMaxFloat(-room_width / 2.0, room_width / 2.0)
            if reconciled.position.x is None else reconciled.position.x
        )
        reconciled.position.y = (0 if reconciled.position.y is None
                                 else reconciled.position.y)
        reconciled.position.z = choose_random(
            MinMaxFloat(-room_length / 2.0, room_length / 2.0)
            if reconciled.position.z is None else reconciled.position.z
        )
        rot = reconciled.rotation_y
        default_wall_scale_x = MinMaxInt(
            ROOM_MIN_XZ,
            room_dim.x if rot in [0, 180] else room_dim.z
        )
        reconciled.wall_scale_x = choose_random(
            default_wall_scale_x
            if reconciled.wall_scale_x is None else
            reconciled.wall_scale_x)

        reconciled.wall_scale_y = choose_random(
            MinMaxInt(ROOM_MIN_Y, room_dim.y)
            if reconciled.wall_scale_y is None else
            reconciled.wall_scale_y)
        return reconciled


class StructuralTurntableCreationService(
        BaseObjectCreationService):

    def __init__(self):
        self._type = FeatureTypes.TURNTABLES
        self._default_template = DEFAULT_TEMPLATE_TURNTABLE
        self._material_tuple = None

    def create_feature_from_specific_values(
            self, scene: Scene, reconciled: StructuralTurntableConfig,
            source_template: StructuralTurntableConfig):
        """Creates a turntable from the given template with
        specific values."""
        if not self._material_tuple:
            self._material_tuple = choose_material_tuple_from_material(
                reconciled.material
            )
        args = {
            'position_x': reconciled.position.x,
            'position_y_modifier': reconciled.position.y,
            'position_z': reconciled.position.z,
            'rotation_y': reconciled.rotation_y,
            'material_tuple': self._material_tuple,
            'radius': reconciled.turntable_radius,
            'height': reconciled.turntable_height,
            'step_begin': reconciled.turntable_movement.step_begin,
            'step_end': reconciled.turntable_movement.step_end,
            'movement_rotation': reconciled.turntable_movement.rotation_y
        }

        logger.trace(f'Creating turntable:\nINPUT = {args}')

        turntable = structures.create_turntable(**args)
        _post_instance(
            scene,
            turntable,
            reconciled,
            source_template,
            self._get_type())
        return turntable

    def _handle_dependent_defaults(
            self, scene: Scene, reconciled: StructuralTurntableConfig,
            source_template: StructuralTurntableConfig
    ) -> StructuralTurntableConfig:

        room_dim = scene.room_dimensions
        def_dim = geometry.DEFAULT_ROOM_DIMENSIONS
        room_width = room_dim.x or def_dim['x']
        room_length = room_dim.z or def_dim['z']
        reconciled = _handle_position_defaults(scene, reconciled)
        # By default, always use the same material for each turntable, but
        # let users configure other materials if desired.
        self._material_tuple = _reconcile_material(
            source_template.material,
            [DEFAULT_TURNTABLE_MATERIAL]
        )
        reconciled.material = self._material_tuple.material

        reconciled.position.x = choose_random(
            MinMaxFloat(-room_width / 2.0, room_width / 2.0)
            if reconciled.position.x is None else reconciled.position.x
        )

        reconciled.position.z = choose_random(
            MinMaxFloat(-room_length / 2.0, room_length / 2.0)
            if reconciled.position.z is None else reconciled.position.z
        )

        # If no Y rotation is set, just ensure that step_end is a number.
        if not reconciled.turntable_movement.rotation_y:
            reconciled.turntable_movement.step_end = (
                reconciled.turntable_movement.step_begin
            )

        end_after = reconciled.turntable_movement.end_after_rotation
        # If end_after_rotation is set but step_end is not set...
        if end_after and not reconciled.turntable_movement.step_end:
            # If % 360 = 0, use 360 instead.
            end_after = (end_after % 360) or 360
            # Set the step_end (remember to subtract 1 since it is inclusive).
            reconciled.turntable_movement.step_end = (
                reconciled.turntable_movement.step_begin - 1 +
                int(end_after / abs(reconciled.turntable_movement.rotation_y))
            )

        return reconciled


class StructuralTubeOccluderCreationService(BaseObjectCreationService):

    def __init__(self):
        self._default_template = DEFAULT_TEMPLATE_TUBE_OCCLUDER
        self._type = FeatureTypes.TUBE_OCCLUDERS
        self._material_tuple = None

    def create_feature_from_specific_values(
        self,
        scene: Scene,
        reconciled: StructuralTubeOccluderConfig,
        source_template: StructuralTubeOccluderConfig
    ):
        if not self._material_tuple:
            self._material_tuple = choose_material_tuple_from_material(
                reconciled.material
            )
        args = {
            'down_step': reconciled.down_step,
            'material_tuple': self._material_tuple,
            'radius': reconciled.radius,
            'room_height': scene.room_dimensions.y,
            'position_x': reconciled.position_x,
            'position_z': reconciled.position_z,
            'up_step': reconciled.up_step
        }
        logger.trace(f'Creating tube occluder:\nINPUT = {args}')
        tube_occluder = structures.create_tube_occluder(**args)
        tube_occluder = _post_instance(
            scene,
            tube_occluder,
            reconciled,
            source_template,
            self._get_type()
        )
        return tube_occluder

    def _handle_dependent_defaults(
        self,
        scene: Scene,
        reconciled: StructuralTubeOccluderConfig,
        source_template: StructuralTubeOccluderConfig
    ) -> StructuralTubeOccluderConfig:
        if reconciled.up_after:
            step = get_step_after_movement([
                label for label in return_list(source_template.up_after)
                if label
            ])
            if step >= 1:
                reconciled.up_step = step
        if reconciled.down_after:
            step = get_step_after_movement([
                label for label in return_list(source_template.down_after)
                if label
            ])
            if step >= 1:
                reconciled.down_step = step
                if not (source_template.up_step or source_template.up_after):
                    reconciled.up_step = 0

        limit_x = (scene.room_dimensions.x - reconciled.radius) / 2.0
        limit_z = (scene.room_dimensions.z - reconciled.radius) / 2.0
        reconciled.position_x = (
            reconciled.position_x if reconciled.position_x is not None
            else random.uniform(-limit_x, limit_x)
        )
        reconciled.position_z = (
            reconciled.position_z if reconciled.position_z is not None
            else random.uniform(-limit_z, limit_z)
        )

        self._material_tuple = _reconcile_material(
            source_template.material,
            materials.ROOM_WALL_MATERIALS
        )
        reconciled.material = self._material_tuple.material

        return reconciled


class StructuralNotchedOccluderCreationService(BaseObjectCreationService):
    def __init__(self):
        self._default_template = DEFAULT_TEMPLATE_NOTCHED_OCCLUDER
        self._type = FeatureTypes.NOTCHED_OCCLUDERS
        self._material_tuple = None

    def create_feature_from_specific_values(
        self,
        scene: Scene,
        reconciled: StructuralNotchedOccluderConfig,
        source_template: StructuralNotchedOccluderConfig
    ) -> StructuralNotchedOccluderConfig:
        if not self._material_tuple:
            self._material_tuple = choose_material_tuple_from_material(
                reconciled.material
            )

        args = {
            'occluder_mat': self._material_tuple,
            'room_dimensions': scene.room_dimensions,
            'position_z': reconciled.position_z,
            'height': reconciled.height,
            'platform_height': reconciled.notch_height,
            'platform_width': reconciled.notch_width,
            'down_step': reconciled.down_step,
            'up_step': reconciled.up_step,
        }
        logger.trace(f'Creating notched occluder:\nINPUT = {args}')
        notched_occluder = create_notched_occluder(**args)
        notched_occluder = _post_instance(
            scene,
            notched_occluder,
            reconciled,
            source_template,
            self._get_type()
        )
        return notched_occluder

    def _handle_dependent_defaults(self,
                                   scene: Scene,
                                   reconciled: StructuralNotchedOccluderConfig,
                                   source_template: StructuralNotchedOccluderConfig  # noqa: E501
                                   ) -> StructuralNotchedOccluderConfig:
        limit_z = scene.room_dimensions.z
        # Should always be centered.
        reconciled.position_z = (
            reconciled.position_z if reconciled.position_z is not None
            else random.uniform(-limit_z, limit_z)
        )

        if not self._material_tuple:
            self._material_tuple = _reconcile_material(
                source_template.material,
                materials.ROOM_WALL_MATERIALS
            )
        reconciled.material = self._material_tuple.material

        return reconciled


@dataclass
class PositionableStructuralObjectsConfig(BaseFeatureConfig):
    """Simple class used for user-positionable structural objects."""
    position: RandomizableVectorFloat3d = None
    rotation_y: RandomizableFloat = None
    material: RandomizableString = None


@dataclass
class StructuralWallConfig(PositionableStructuralObjectsConfig):
    """
    Defines details of a structural interior wall.  The wall will be the
    height of the room.

    - `num` (int, or list of ints, or [MinMaxInt](#MinMaxInt) dict, or list of
    MinMaxInt dicts): Number of structures to be created with these parameters
    - `labels` (string, or list of strings): A label or labels to be assigned
    to this object. Always automatically assigned "walls"
    - `material` (string, or list of strings): The structure's material or
    material type.
    - `position` ([VectorFloatConfig](#VectorFloatConfig) dict, or list of
    VectorFloatConfig dicts): The structure's position in the scene
    - `rotation_y` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat)
    dict, or list of MinMaxFloat dicts): The structure's rotation in the scene
    - `width` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat) dict,
    or list of MinMaxFloat dicts): The width of the wall.
    Default: Between 5% and 50% of the room's maximum dimensions.
    - `thickness` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat)
    dict, or list of MinMaxFloat dicts): The thickness of the wall.
    Default: 0.1
    - `height` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat)
    dict, or list of MinMaxFloat dicts): The height of the wall.
    Default: The room's height.
    - `same_material_as_room` (bool, or list of bools): Whether to use the same
    material as the room's walls. If true, will ignore any `material`
    configured on this wall. Default: false
    - `ignore_bounds` (bool, or list of bools): Whether to ignore the bounds of
    all other objects that have already been generated at the moment this wall
    is being generated. Default: false
    """
    width: RandomizableFloat = None
    thickness: RandomizableFloat = None
    height: RandomizableFloat = None
    same_material_as_room: RandomizableBool = None
    ignore_bounds: RandomizableBool = None


@dataclass
class StructuralPlatformLipsConfig():
    """
    Defines the platform's lips with front, back,
    left, and right configurations.

    - `front` (bool) : Positive Z axis
    - `back` (bool) : Negative Z axis
    - `left` (bool) : Negative X axis
    - `right` (bool) : Positive X axis
    """
    front: bool = None
    back: bool = None
    left: bool = None
    right: bool = None


@dataclass
class StructuralPlatformConfig(PositionableStructuralObjectsConfig):
    """
    Defines details of a structural platform. The top of a platform should
    never exceed room_dimension_y - 1.25 if a target or performer are to be
    placed on top of the platform to ensure the performer can reach the target.

    - `num` (int, or list of ints, or [MinMaxInt](#MinMaxInt) dict, or list of
    MinMaxInt dicts): Number of structures to be created with these parameters
    - `attached_ramps` (int, or list of ints, or [MinMaxInt](#MinMaxInt) dict,
    or list of MinMaxInt dicts): Number of ramps that should be attached to
    this platform to allow the performer to climb up to this platform.
    Default: 0
    - `labels` (string, or list of strings): A label or labels to be assigned
    to this object. Always automatically assigned "platforms"
    - `lips` ([StructuralPlatformLipsConfig]
    (#StructuralPlatformLipsConfig), or list of
    StructuralPlatformLipsConfig): The platform's lips. If set, the platform's
    X/Z scales will be increased to 0.8 if lower than 0.8. Default: no lips
    - `material` (string, or list of strings): The structure's material or
    material type.
    - `platform_underneath` (bool or list of bools): If true, add a platform
    below this platform that touches the floor on the bottom and this platform
    on the top.  This platform will fully be encased in the x/z directions by
    the platform created underneath.  Default: False
    - `platform_underneath_attached_ramps` (int, or list of ints, or
    [MinMaxInt](#MinMaxInt) dict, or list of MinMaxInt dicts): Number of ramps
    that should be attached to the platform created below this platform to
    allow the performer to climb onto that platform. Default: 0
    - `position` ([VectorFloatConfig](#VectorFloatConfig) dict, or list of
    VectorFloatConfig dicts): The structure's position in the scene
    - `rotation_y` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat)
    dict, or list of MinMaxFloat dicts): The structure's rotation in the scene
    - `scale` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat) dict,
    or list of MinMaxFloat dicts): Scale of the platform
    - `auto_adjust_platforms` (bool or list of bools): If true, makes sure all
    platform heights do not exceed 1.25 units below the y room dimension
    allowing the performer to always stand on top of a platform. For example,
    a room with a room_dimension_y = 4 and
    auto_adjusted_platforms = True will ensure that all platform heights
    do not exceed 2.75.  Default: False
    - `long_with_two_ramps` (bool or list of bools): If true makes the platform
    extend across the entire length of the room in either the x or z dimension.
    The platform will have lips automatically added on all sides.
    A single ramp will be added on opposite sides of the platform totalling to
    two connected ramps. With larger room sizes the max number of these types
    of platforms is 3 though 1 is recommended for smaller room sizes.
    When combined with `platform_underneath` there will be a smaller platform
    of the same type stacked on top totalling 2 platforms and 4 ramps in the
    scene. Only one instance of a platform combined with `platform_underneath`
    can be added to the scene instead of the 3 allowed without a
    `platform_underneath`. This property will override `attached_ramps`, and
    `platform_underneath_attached_ramps`
    Default: False
    - `adjacent_to_wall` (string or list of strings): Will place the platform
    directly adjacent to a wall or in the corner of the room. Can be combined
    with `platform_underneath` to place a stacked platform adjacent to a wall.
    Multiple platforms can be placed against the same wall or different walls
    but only one platform can be placed in each corner. Setting this will
    override `rotation_y`.
    Options available: 'left', 'right', 'front', 'back', 'front_left_corner',
    'front_right_corner', 'back_left_corner', 'back_right_corner'.
    Default: None
    """
    lips: Union[StructuralPlatformLipsConfig,
                List[StructuralPlatformLipsConfig]] = None
    scale: RandomizableVectorFloat3dOrFloat = None
    attached_ramps: RandomizableInt = None
    platform_underneath: RandomizableBool = None
    platform_underneath_attached_ramps: RandomizableInt = None  # noqa
    auto_adjust_platforms: RandomizableBool = False
    long_with_two_ramps: RandomizableBool = False
    adjacent_to_wall: RandomizableString = None


@dataclass
class StructuralRampConfig(PositionableStructuralObjectsConfig):
    """
    Defines details of a structural ramp.

    - `num` (int, or list of ints, or [MinMaxInt](#MinMaxInt) dict, or list of
    MinMaxInt dicts): Number of structures to be created with these parameters
    - `angle` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat) dict,
    or list of MinMaxFloat dicts): Angle of the ramp upward from the floor
    - `labels` (string, or list of strings): A label or labels to be assigned
    to this object. Always automatically assigned "ramps"
    - `length` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat) dict,
    or list of MinMaxFloat dicts): Length of the ramp along the floor.  This
    is the 'adjacent' side and not the hypotenuse.
    - `material` (string, or list of strings): The structure's material or
    material type.
    - `platform_underneath` (bool or list of bools): If true, add a platform
    below this ramp that touches the floor on the bottom and the bottom of
    this ramp on the top.  This ramp will fully be encased in the x/z
    directions by the platform created underneath.  Default: False
    - `platform_underneath_attached_ramps` (int, or list of ints, or
    [MinMaxInt](#MinMaxInt) dict, or list of MinMaxInt dicts): Number of ramps
    that should be attached to the platform created below this ramp to
    allow the performer to climb onto that platform.  Default: 0
    - `position` ([VectorFloatConfig](#VectorFloatConfig) dict, or list of
    VectorFloatConfig dicts): The structure's position in the scene
    - `rotation_y` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat)
    dict, or list of MinMaxFloat dicts): The structure's rotation in the scene
    - `width` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat) dict,
    or list of MinMaxFloat dicts): Width of the ramp
    """
    angle: RandomizableFloat = None
    width: RandomizableFloat = None
    length: RandomizableFloat = None
    platform_underneath: RandomizableBool = None
    platform_underneath_attached_ramps: RandomizableInt = None  # noqa


@dataclass
class StructuralLOccluderConfig(PositionableStructuralObjectsConfig):
    """
    Defines details of a structural L-shaped occluder.

    - `num` (int, or list of ints, or [MinMaxInt](#MinMaxInt) dict, or list of
    MinMaxInt dicts): Number of structures to be created with these parameters
    - `backwards` (bool, or list of bools): Whether to create a backwards L.
    Default: [true, false]
    - `labels` (string, or list of strings): A label or labels to be assigned
    to this object. Always automatically assigned "l_occluders"
    - `material` (string, or list of strings): The structure's material or
    material type.
    - `position` ([VectorFloatConfig](#VectorFloatConfig) dict, or list of
    VectorFloatConfig dicts): The structure's position in the scene
    - `rotation_y` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat)
    dict, or list of MinMaxFloat dicts): The structure's rotation in the scene
    - `scale_front_x` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat)
    dict, or list of MinMaxFloat dicts): Scale in the x direction of the front
    part of the occluder
    - `scale_front_z` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat)
    dict, or list of MinMaxFloat dicts): Scale in the z direction of the front
    part of the occluder
    - `scale_side_x` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat)
    dict, or list of MinMaxFloat dicts): Scale in the x direction of the side
    part of the occluder
    - `scale_side_z` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat)
    dict, or list of MinMaxFloat dicts): Scale in the z direction of the side
    part of the occluder
    - `scale_y` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat)
    dict, or list of MinMaxFloat dicts): Scale in the y direction for the
    entire occluder
    """
    backwards: RandomizableBool = None
    scale_front_x: RandomizableFloat = None
    scale_front_z: RandomizableFloat = None
    scale_side_x: RandomizableFloat = None
    scale_side_z: RandomizableFloat = None
    scale_y: RandomizableFloat = None


@dataclass
class StructuralDropperConfig(BaseFeatureConfig):
    """
    Defines details of a structural dropper and its dropped projectile.

    - `num` (int, or list of ints, or [MinMaxInt](#MinMaxInt) dict, or list of
    MinMaxInt dicts): Number of structures to be created with these parameters
    - `drop_step` (int, or list of ints, or [MinMaxInt](#MinMaxInt) dict, or
    list of MinMaxInt dicts): The step of the simulation in which the
    projectile should be dropped.
    - `labels` (string, or list of strings): A label or labels to be assigned
    to this object. Always automatically assigned "droppers"
    - `position_relative` ([RelativePositionConfig](#RelativePositionConfig)
    dict, or list of RelativePositionConfig dicts): Configuration options for
    positioning this object relative to another object, rather than using
    `position_x` or `position_z`. If configuring this as a list, then all
    listed options will be applied to each scene in the listed order, with
    later options overriding earlier options if necessary. Default: not used
    - `position_x` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat)
    dict, or list of MinMaxFloat dicts): Position in the x direction of the of
    the ceiling where the dropper should be placed.
    - `position_z` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat)
    dict, or list of MinMaxFloat dicts): Position in the z direction of the of
    the ceiling where the dropper should be placed.
    - `projectile_dimensions` (float, or list of floats, or
    [MinMaxFloat](#MinMaxFloat) dict, or list of MinMaxFloat dicts): Dimensions
    of the projectile. Overrides the `projectile_scale` if configured.
    Default: use `projectile_scale`
    - `projectile_labels` (string, or list of strings): A label for an existing
    object in your ILE configuration that will be used as this device's
    projectile, or new label(s) to associate with a new projectile object.
    Other configuration options may use this label to reference this object or
    a group of objects. Labels are not unique, and when multiple objects share
    labels, the ILE may choose one available object or all of them, depending
    on the specific option. The ILE will ignore any objects that have keyword
    locations or are used by other droppers/placers/throwers.
    - `projectile_material` (string, or list of strings): The projectiles's
    material or material type.
    - `projectile_scale` (float, or list of floats, or
    [MinMaxFloat](#MinMaxFloat) dict, or list of MinMaxFloat dicts): Scale of
    the projectile. Default is based on the shape.
    - `projectile_shape` (string, or list of strings): The shape or type of
    the projectile.
    - `no_projectile` (bool, or list of bools): If `true`, this device will
    not be holding a projectile; other "projectile" options will be ignored.
    Default: `false`
    """
    position_x: RandomizableFloat = None
    position_z: RandomizableFloat = None
    drop_step: RandomizableInt = None
    no_projectile: RandomizableBool = False
    projectile_dimensions: RandomizableVectorFloat3dOrFloat = None
    projectile_labels: RandomizableString = None
    projectile_material: RandomizableString = None
    projectile_scale: RandomizableVectorFloat3dOrFloat = None
    projectile_shape: RandomizableString = None
    position_relative: Union[
        RelativePositionConfig,
        List[RelativePositionConfig]
    ] = None


@dataclass
class StopPositionConfig():
    """
    Set a stop position for a thrown object. Works best with non-spherical,
    non-cylindrical shapes, like toys with wheels.

    - `x` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat)
    dict, or list of MinMaxFloat dicts): The stop X position. Must also
    configure `z`. Default: null
    - `z` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat)
    dict, or list of MinMaxFloat dicts): The stop Z position. Must also
    configure `x`. Default: null
    - `behind` (str, or list of strs): The label for an existing object behind
    which this object should stop. Only works if `passive_physics_scene` is
    `true`. Overrides the `x` and `z` options. Useful for stopping objects
    behind occluders. Default: null
    - `offscreen` (bool, or list of bools): Sets the stop position offscreen,
    out of view of the performer agent. Only works if `passive_physics_scene`
    is `true`. Overrides the `x` and `z` options. Default: `false`
    """
    x: RandomizableFloat = None
    z: RandomizableFloat = None
    behind: RandomizableString = None
    offscreen: RandomizableBool = None


@dataclass
class RelativePathConfig():
    """
    Defines details of an object's movement path as it relates to another
    object, for the moving object to avoid or collide with the other object.

    - `labels` (string, or list of strings): The label of the object that the
    moving object should avoid or collide with. Required.
    - `option` (string, or list of strings): Whether the moving object should
    avoid or collide with the other object. Options: `"avoid"`, `"collide"`.
    Default: null
    """
    option: RandomizableString = None
    labels: RandomizableString = None


@dataclass
class StructuralThrowerConfig(BaseFeatureConfig):
    """
    Defines details of a structural dropper and its thrown projectile.

    - `num` (int, or list of ints, or [MinMaxInt](#MinMaxInt) dict, or list of
    MinMaxInt dicts): Number of structures to be created with these parameters
    - `height` (float, or list of floats, or
    [MinMaxFloat](#MinMaxFloat) dict, or list of MinMaxFloat dicts): The
    height on the wall that the thrower will be placed.
    - `impulse` (bool, or list of bools): Whether to use "impulse" force mode.
    We recommend using impulse force mode moving forward. Please note that the
    default `throw_force` is different for impulse and non-impulse force modes.
    Default: true
    - `labels` (string, or list of strings): A label or labels to be assigned
    to this object. Always automatically assigned "throwers"
    - `passive_physics_collision_force` (bool, or list of bools): Automatically
    set the `throw_force` to a speed normally used in a passive physics
    collision scene. If set, overrides the `throw_force`. Default: false
    - `passive_physics_setup` (string, or list of strings): Automatically set
    the `wall`, `position_wall`, `rotation_y, `rotation_z`, and `height` to
    values normally used in passive physics non-collision scenes. If set, this
    will override other config options (see below). Possible settings:
    `"random"`, `"roll_angled"`, `"roll_straight"`, `"toss_straight"`.
    Default: null
      - `wall: ['left', 'right']`
      - If `"roll_angled"`: a `height` of `0` and a `rotation_y` of either
        `-20` or `20`
      - If `"roll_straight"`: a `height` of `0` and a `rotation_y` of `0`
      - If `"toss_straight"`: a `height` of `1` and a `rotation_y` of `0`
      - If `"roll_angled"`: a `position_wall` of `1.6` if `rotation_y` is `-20`
       or `5.6` if `rotation_y` is `20`
      - If `"roll_straight"` or `"toss_straight"`: a `position_wall` between
        `1.6` and `4.4`
    - `passive_physics_throw_force` (bool, or list of bools): Automatically set
    the `throw_force` to a speed normally used in passive physics non-collision
    scene. If set, overrides the `throw_force`. Default: false
    - `path_relative` ([RelativePathConfig](#PathRelativeConfig) dict):
    Configuration options for rotating this thrower so the thrown object will
    either collide with, or avoid, another object. Overrides the `rotation_y`.
    Default: not used
    - `position_relative` ([RelativePositionConfig](#RelativePositionConfig)
    dict, or list of RelativePositionConfig dicts): Configuration options for
    positioning this object relative to another object, rather than using
    `position_wall`. If configuring this as a list, then all listed options
    will be applied to each scene in the listed order, with later options
    overriding earlier options if necessary. Default: not used
    - `position_wall` (float, or list of floats, or
    [MinMaxFloat](#MinMaxFloat) dict, or list of MinMaxFloat dicts): The
    position along the wall that the thrower will be placed.
    - `projectile_dimensions` (float, or list of floats, or
    [MinMaxFloat](#MinMaxFloat) dict, or list of MinMaxFloat dicts): Dimensions
    of the projectile. Overrides the `projectile_scale` if configured.
    Default: use `projectile_scale`
    - `projectile_labels` (string, or list of strings): A label for an existing
    object in your ILE configuration that will be used as this device's
    projectile, or new label(s) to associate with a new projectile object.
    Other configuration options may use this label to reference this object or
    a group of objects. Labels are not unique, and when multiple objects share
    labels, the ILE may choose one available object or all of them, depending
    on the specific option. The ILE will ignore any objects that have keyword
    locations or are used by other droppers/placers/throwers.
    - `projectile_material` (string, or list of strings): The projectiles's
    material or material type.
    - `projectile_scale` (float, or list of floats, or
    [MinMaxFloat](#MinMaxFloat) dict, or list of MinMaxFloat dicts): Scale of
    the projectile. Default is based on the shape.
    - `projectile_shape` (string, or list of strings): The shape or type of
    the projectile.
    - `rotation_y` (float, or list of floats, or
    [MinMaxFloat](#MinMaxFloat) dict, or list of MinMaxFloat dicts): The angle
    in which the thrower will be rotated from its original position on the wall
    to point sideways (either left or right), with 0 being the center.
    This value should be between -45 and 45. Default: random value between
    -45 and 45.
    - `rotation_z` (float, or list of floats, or
    [MinMaxFloat](#MinMaxFloat) dict, or list of MinMaxFloat dicts): The angle
    in which the thrower will be rotated to point upwards.  This value should
    be between 0 and 15. Default: random value between 0 and 15.
    - `stop_position` ([StopPositionConfig](#StopPositionConfig) dict, or list
    of StopPositionConfig dicts): Sets a stop position for the thrown object.
    If set, overrides all other "throw force" options.
    - `throw_force` (float, or list of floats, or
    [MinMaxFloat](#MinMaxFloat) dict, or list of MinMaxFloat dicts): Force of
    the throw put on the projectile.  This value will be multiplied by the
    mass of the projectile.  Default: between 5 and 20 for impulse force mode,
    or between 500 and 2000 for non-impulse force mode
    - `throw_force_multiplier` (float, or list of floats, or
    [MinMaxFloat](#MinMaxFloat) dict, or list of MinMaxFloat dicts): Force of
    the throw put on the projectile, that will be multiplied by the appropriate
    room dimension for the thrower's wall position (X for left/right, Z for
    front/back). If set, overrides the `throw_force`.
    - `throw_step` (int, or list of ints, or [MinMaxInt](#MinMaxInt) dict, or
    list of MinMaxInt dicts): The step of the simulation in which the
    projectile should be thrown. Please note that using a value of less than 5
    may cause unexpected behavior, so we recommend using values of 5 or more in
    your custom config files.
    - `wall` (string, or list of strings): Which wall the thrower should be
    placed on.  Options are: left, right, front, back.
    - `no_projectile` (bool, or list of bools): If `true`, this device will
    not be holding a projectile; other "projectile" options will be ignored.
    Default: `false`
    """
    wall: RandomizableString = None
    position_wall: RandomizableFloat = None
    height: RandomizableFloat = None
    rotation_y: RandomizableFloat = None
    rotation_z: RandomizableFloat = None
    throw_step: RandomizableInt = None
    throw_force: RandomizableFloat = None
    no_projectile: RandomizableBool = False
    projectile_dimensions: RandomizableVectorFloat3dOrFloat = None
    projectile_labels: RandomizableString = None
    projectile_material: RandomizableString = None
    projectile_scale: RandomizableVectorFloat3dOrFloat = None
    projectile_shape: RandomizableString = None
    position_relative: Union[
        RelativePositionConfig,
        List[RelativePositionConfig]
    ] = None
    impulse: RandomizableBool = True
    throw_force_multiplier: RandomizableFloat = None
    passive_physics_collision_force: RandomizableBool = False
    passive_physics_setup: RandomizableString = None
    passive_physics_throw_force: RandomizableBool = False
    stop_position: Union[StopPositionConfig, List[StopPositionConfig]] = None
    path_relative: RelativePathConfig = None


@dataclass
class StructuralMovingOccluderConfig(BaseFeatureConfig):
    """
    Defines details of a structural moving occluder.

    - `num` (int, or list of ints, or [MinMaxInt](#MinMaxInt) dict, or list of
    MinMaxInt dicts): Number of structures to be created with these parameters
    - `labels` (string, or list of strings): A label or labels to be assigned
    to this object. Always automatically assigned "moving_occluders"
    - `occluder_height` (float, or list of floats, or
    [MinMaxFloat](#MinMaxFloat) dict, or list of MinMaxFloat dicts): Height (Y
    scale) of the occluder wall.  Default is between .25 and 2.5.
    - `occluder_thickness` (float, or list of floats, or
    [MinMaxFloat](#MinMaxFloat) dict, or list of MinMaxFloat dicts): Thickness
    (Z scale) of the occluder wall.  Default is between .02 and 0.5.
    - `occluder_width` (float, or list of floats, or
    [MinMaxFloat](#MinMaxFloat) dict, or list of MinMaxFloat dicts): Width (X
    scale) of the occluder wall.  Default is between .25 and 4.
    - `origin` (string, or list of strings): Location that the occluder's pole
    will originate from.  Options are `top`, `front`, `back`, `left`, `right`.
    Default is weighted such that `top` occurs 50% of the time and the sides
    are each 12.5%.  Users can weight options by included them more than once
    in an array.  For example, the default can be represented as:
    ```
    ['top', 'top', 'top', 'top', 'front', 'back', 'left', 'right']
    ```
    - `pole_material` (string, or list of strings): Material of the occluder
    pole (cylinder)
    - `position_x` (float, or list of floats, or
    [MinMaxFloat](#MinMaxFloat) dict, or list of MinMaxFloat dicts): X
    position of the center of the occluder
    - `position_z` (float, or list of floats, or
    [MinMaxFloat](#MinMaxFloat) dict, or list of MinMaxFloat dicts): Z
    position of the center of the occluder
    - `move_down_only` (bool, or list of bools): If true, occluder will start
    near the ceiling, moving downwards until it touches the floor with no
    rotation. Note that if this is true, the following settings are ignored:
    `move_up_before_last_step`, `repeat_movement`, and `reverse_direction`.
    Default: false
    - `move_up_before_last_step` (bool, or list of bools): If true, repeat the
    occluder's full movement and rotation before the scene's last step. Ignored
    if `last_step` isn't configured, or if `repeat_movement` is true.
    Default: false
    - `repeat_interval` (int, or list of ints, or [MinMaxInt](#MinMaxInt) dict,
    or list of MinMaxInt dicts): if `repeat_movement` is true, the number of
    steps to wait before repeating the full movement.  Default is between 1
    and 20.
    - `repeat_movement` (bool, or list of bools): If true, repeat the
    occluder's full movement and rotation indefinitely, using `repeat_interval`
    as the number of steps to wait. Default: false
    - `reverse_direction` (bool, or list of bools): Reverse the rotation
    direction of a sideways wall by rotating the wall 180 degrees. Only used if
    `origin` is set to a wall and not `top`. Default: [true, false]
    - `rotation_y` (float, or list of floats, or [MinMaxInt](#MinMaxFloat)
    dict, or list of MinMaxFloat dicts): Y rotation of a non-sideways occluder
    wall; only used if any `origin` is set to `top`.  Default is 0 to 359.
    - `wall_material` (string, or list of strings): Material of the occluder
    wall (cube)
    """

    wall_material: RandomizableString = None
    pole_material: RandomizableString = None
    position_x: RandomizableFloat = None
    position_z: RandomizableFloat = None
    origin: RandomizableString = None
    occluder_height: RandomizableFloat = None
    occluder_width: RandomizableFloat = None
    occluder_thickness: RandomizableFloat = None
    repeat_movement: RandomizableBool = None
    repeat_interval: RandomizableInt = None
    reverse_direction: RandomizableBool = None
    rotation_y: RandomizableFloat = None
    move_up_before_last_step: RandomizableBool = None
    move_down_only: RandomizableBool = None


@dataclass
class PartitionFloorConfig():
    leftHalf: Optional[float] = None
    rightHalf: Optional[float] = None


@dataclass
class FloorAreaConfig(BaseFeatureConfig):
    """Defines an area of the floor of the room.  Note: Coordinates must be
    integers. Areas are always size 1x1 centered on the given X/Z coordinate.
    Adjacent areas are combined.

    - `num` (int, or list of ints, or [MinMaxInt](#MinMaxInt) dict, or list of
    MinMaxInt dicts): Number of areas to be used with these parameters
    - `position_x` (int, or list of ints, or [MinMaxInt](#MinMaxInt) dict, or
    list of MinMaxInt dicts): X position of the area.
    - `position_z` (int, or list of ints, or [MinMaxInt](#MinMaxInt) dict, or
    list of MinMaxInt dicts): Z position of the area.
    - `size` (int, or list of ints, or [MinMaxInt](#MinMaxInt) dict, or
    list of MinMaxInt dicts): Size of a single contiguous area. If `position_x`
    and/or `position_z` are set, the area will include a space matching those
    coordinate(s). Default: 1
    """
    position_x: RandomizableInt = None
    position_z: RandomizableInt = None
    size: RandomizableInt = None


@dataclass
class FloorMaterialConfig(FloorAreaConfig):
    """Defines details of a specific material on a specific location of the
    floor.  Be careful if num is greater than 1, be sure there are
    possibilities such that enough floor areas can be generated.
    Note: Coordinates must be integers. Areas are always size 1x1 centered on
    the given X/Z coordinate. Adjacent areas are combined.

    - `num` (int, or list of ints, or [MinMaxInt](#MinMaxInt) dict, or list of
    MinMaxInt dicts): Number of areas to be used with these parameters
    - `material` (string, or list of strings): The floor's material or
    material type.
    - `position_x` (int, or list of ints, or [MinMaxInt](#MinMaxInt) dict, or
    list of MinMaxInt dicts): X position of the area.
    - `position_z` (int, or list of ints, or [MinMaxInt](#MinMaxInt) dict, or
    list of MinMaxInt dicts): Z position of the area.
    """
    material: RandomizableString = None


@dataclass
class StructuralOccludingWallConfig(PositionableStructuralObjectsConfig):
    """
     - `num` (int, or list of ints, or [MinMaxInt](#MinMaxInt) dict, or list of
    MinMaxInt dicts): Number of walls to be created with these parameters
    - `keyword_location`: ([KeywordLocationConfig](#KeywordLocationConfig)):
    Used to identify one of the qualitative locations specified by keywords.
    This field should not be set when `position` or `rotation` are also set.
    - `labels` (string, or list of strings): A label or labels to be assigned
    to this object. Always automatically assigned "occluding_walls"
    - `material` (string, or list of strings): The wall's material or
    material type.
    - `position` ([VectorFloatConfig](#VectorFloatConfig) dict, or list of
    VectorFloatConfig dicts): The wall's position in the scene.  Will be
    overrided by keyword location.
    - `rotation_y` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat)
    dict, or list of MinMaxFloat dicts): The wall's rotation in the scene.
    Will be overrided by keyword location.
    - `scale` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat) dict,
    or list of MinMaxFloat dicts): Scale of the wall.  Default is scaled to
    target size.  This will override the scale provided by the `type` field.
    - `type` (string, or list of strings): describes the type of occluding
    wall. Types include:
      `occludes` - occludes the target or object.
      `short` - wide enough, but not tall enough to occlude the target.
      `thin` - tall enough, but not wide enough to occlude the target.
      `hole` - wall with a hole that reveals the target.
    Default: ['occludes', 'occludes', 'occludes', 'short', 'thin', 'hole']
    """
    type: RandomizableString = None
    scale: RandomizableVectorFloat3dOrFloat = None
    keyword_location: Union[KeywordLocationConfig,
                            List[KeywordLocationConfig]] = None


@ dataclass
class StructuralPlacerConfig(BaseFeatureConfig):
    """Defines details for an instance of a placer (cylinder) descending from
    the ceiling on the given activation step to place an object with the given
    position. For some object shapes (specifically `container_symmetric_*` and
    `container_asymmetric_*`), two placers will be made and attached to the
    object.

    - `num` (int, or list of ints, or [MinMaxInt](#MinMaxInt) dict, or list of
    MinMaxInt dicts): Number of areas to be used with these parameters
    - `activate_after`: (str, or list of strs): Overrides the `activation_step`
    (overriding the manual config) based on the movement of other object(s) in
    the scene. Should be set to one or more labels for mechanical objects that
    may move or rotate, like placers or turntables, as well as agents. The
    `activation_step` of this object will be set to the step immediately after
    ALL of the objects and agents finish moving and rotating. If multiple
    labels are configured, all labels will be used. Default: Use
    `activation_step`
    - `activate_on_start_or_after`: (str, or list of strs, or bool): Overrides
    the `activation_step` (overriding the manual config) based on the movement
    of other object(s) in the scene. Should be set to one or more labels for
    mechanical objects that can move or rotate, like placers or turntables, as
    well as agents. If ANY of the objects or agents begin moving or rotating
    immediately at the start of the scene (step 1), then the `activation_step`
    of this object will be set to the step immediately after ALL of the objects
    and agents finish moving and rotating; otherwise, if ALL of the objects and
    agents begin moving and rotating after step 1, then the `activation_step`
    of this object will be set to 1. If multiple labels are configured, all
    labels will be used. Default: Use `activation_step`
    - `activation_step`: (int, or list of ints, or [MinMaxInt](#MinMaxInt)
    dict, or list of MinMaxInt dicts): Step on which the placer should begin
    its downward movement. Default: between 0 and 10
    - `deactivation_step`: (int, or list of ints, or [MinMaxInt](#MinMaxInt)
    dict, or list of MinMaxInt dicts): Step on which the placer should release
    its held object. This number must be a step after the end of the placer's
    downward movement. Default: At the end of the placer's downward movement
    - `empty_placer` (bool, or list of bools): If True, the placer will not
    hold, drop, or move an object. Use 'placed_object_position' to set the
    placer's starting X/Z position if needed. Cannot be used in combination
    with any of the following config options: 'placed_object_labels',
    'placed_object_material', 'placed_object_rotation', 'placed_object_scale',
    'placed_object_shape'. Default: False
    - `end_height`: (float, or list of floats, or [MinMaxFloat](#MinMaxFloat)
    dict): Height at which the placer should release its held object.
    Alternatively, one can use the `end_height_relative_object_label`.
    Default: 0 (so the held object is in contact with the floor)
    - `end_height_relative_object_label` (string): Label used to match
    the bottom of the object held by the placer to the height of another
    (for example, the support platform in gravity scenes). This will override
    `end_height` if both are set.
    - `existing_object_required` (bool, or list of bools): If this is `true`
    and `placed_object_labels` is set, this placer will be required to use an
    existing object with the configured label. If this is `false`, and no
    objects exist with the `placed_object_labels`, then the placer will
    automatically generate a new object and assign it all the
    `placed_object_labels`. Default: False
    - `labels` (string, or list of strings): A label or labels to be assigned
    to this object. Always automatically assigned "placers"
    - `move_object` (bool): If True, the placer will move the object on the X
    axis from its `placed_object_position` to `move_object_end_position`.
    Default: False
    - `move_object_end_position`: ([VectorFloatConfig](#VectorFloatConfig)
    dict, or list of VectorFloatConfig dicts): If `move_object` is True,
    the placer will move the object from its `placed_object_position` to the
    X position in `move_object_end_position`. Note that the Y and Z positions
    in `move_object_end_position` are ignored; the Y and Z positions set in
    `placed_object_position` are kept the same. (This option remains a Vector
    for backwards compatibility.)
    - `move_object_y`: If `move_object` is True, the placer will raise the
    object by this amount before moving it on the X/Z axes, and lower it by
    the same amount afterward. Default: `0`
    - `move_object_z`: If `move_object` is True, the placer will move the
    object by this amount in the -Z direction after raising it on the Y axis
    but before moving it on the X axis, and move it by the same amount again
    in the +Z direction after moving it on the X axis and before lowering it
    on the Y axis. Default: `1.5`
    - `pickup_object` (bool): If True, the placer will pickup the object.
    Default: False
    - `placed_object_above` (string, or list of strings): A label for an
    existing object in your configuration whose X/Z position will be used for
    this placer's (and the placed object's) starting position. Overrides
    `placed_object_position`. Please use `end_height_relative_object_label` if
    you need to set the held object's ending Y position.
    - `placed_object_labels` (string, or list of strings): A label for an
    existing object in your configuration that will be used as this placer's
    placed object, or new label(s) to associate with a new placed object.
    Other configuration options may use this label to reference this object or
    a group of objects. Labels are not unique, and when multiple objects share
    labels, the ILE may choose one available object or all of them, depending
    on the specific option. The ILE will ignore any objects that have keyword
    locations or are used by other droppers/placers/throwers.
    - `placed_object_material` (string, or list of strings): The material
    (color/texture) to use on the placed object in each scene. For a list, a
    new material will be randomly chosen for each scene. Default: random
    - `placed_object_position`: ([VectorFloatConfig](#VectorFloatConfig) dict,
    or list of VectorFloatConfig dicts): The placed object's position in the
    scene
    - `placed_object_rotation`: (float, or list of floats, or
    [MinMaxFloat](#MinMaxFloat) dict, or list of MinMaxFloat dicts): The placed
    object's rotation on the y axis.
    - `placed_object_scale`: (float, or list of floats, or
    [MinMaxFloat](#MinMaxFloat) dict, or list of MinMaxFloat dicts): Placed
    object's scale.  Default is a value between 0.2 and 2.
    - `placed_object_shape` (string, or list of strings): The shape (object
    type) of the placed object. For a list, a new shape will be randomly
    chosen for each scene. Default: random
    - `position_relative` ([RelativePositionConfig](#RelativePositionConfig)
    dict, or list of RelativePositionConfig dicts): Configuration options for
    positioning this object relative to another object, rather than using
    `placed_object_position`. If configuring this as a list, then all
    listed options will be applied to each scene in the listed order, with
    later options overriding earlier options if necessary. Default: not used
    - `randomize_once` (StructuralPlacerConfig dict) Placer configuration
    options that are only randomized once. If this configuration template would
    generate more than one placer (has a `num` greater than 1), then each
    placer generated by this template will use the same randomized values for
    all the options in `randomize_once`. Default: not used
    - `retain_position` (bool, or list of bools): The placed object will
    retain its current X/Z position. Overrides `placed_object_position` and
    `placed_object_above'. Default: False
    """

    num: RandomizableInt = 0
    activation_step: RandomizableInt = None
    end_height: RandomizableFloat = None
    end_height_relative_object_label: str = None
    placed_object_position: RandomizableVectorFloat3d = None
    placed_object_scale: RandomizableVectorFloat3dOrFloat = None
    placed_object_rotation: RandomizableFloat = None
    placed_object_shape: RandomizableString = None
    placed_object_material: RandomizableString = None
    placed_object_labels: RandomizableString = None
    deactivation_step: RandomizableInt = None
    position_relative: Union[
        RelativePositionConfig,
        List[RelativePositionConfig]
    ] = None
    empty_placer: RandomizableBool = False
    pickup_object: bool = False
    move_object: bool = False
    move_object_end_position: RandomizableVectorFloat3d = None
    move_object_y: RandomizableFloat = None
    move_object_z: RandomizableFloat = None
    placed_object_above: RandomizableString = None
    activate_after: RandomizableString = None
    activate_on_start_or_after: RandomizableString = None
    existing_object_required: RandomizableBool = False
    retain_position: RandomizableBool = False


@dataclass
class StructuralDoorConfig(PositionableStructuralObjectsConfig):
    """
    Defines details of a structural door that can be opened and closed.

    - `num` (int, or list of ints, or [MinMaxInt](#MinMaxInt) dict, or list of
    MinMaxInt dicts): Number of structures to be created with these parameters
    - `labels` (string, or list of strings): A label or labels to be assigned
    to this object. Always automatically assigned "doors"
    - `material` (string, or list of strings): The structure's material or
    material type.
    - `position` ([VectorFloatConfig](#VectorFloatConfig) dict, or list of
    VectorFloatConfig dicts): The structure's position in the scene
    - `rotation_y` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat)
    dict, or list of MinMaxFloat dicts): The structure's rotation in the scene.
    For doors, must be 0, 90, 180, or 270
    - `wall_material` (string, or list of strings): The material for the wall
    around the door.
    - `wall_scale_x` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat)
    dict, or list of MinMaxFloat dicts): Scale of the walls around the door in
    the x direction.  Default: A random value between 2 and the size of the
    room in the direction parallel with the door and wall.
    - `wall_scale_y` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat)
    dict, or list of MinMaxFloat dicts): Scale of the walls around the door in
    the y direction.  The door will be 2 units high, so this scale must be
    greater than 2 for the top wall to appear.  Default: A random value between
    2 and the height of the room.
    """
    wall_material: RandomizableString = None
    wall_scale_x: RandomizableFloat = None
    wall_scale_y: RandomizableFloat = None


@dataclass
class StructuralObjectMovementConfig():
    """
    Represents what movements the structural object will make. Currently
    only used for configuring turntables.

    - `end_after_rotation` (int, or list of ints, or [MinMaxInt](#MinMaxInt)
    dict, or list of MinMaxInt dicts): The amount the structure will rotate in
    full, beginning on `step_begin` and rotating `rotation_y` each step.
    All values will be % 360 (set 360 for a complete rotation). If
    `end_after_rotation` does not divide evenly by `rotation_y`, then the
    number of rotation steps will round down. Default is either 90, 180, 270,
    or 360.
    - `step_begin` (int, or list of ints, or [MinMaxInt](#MinMaxInt) dict):
    The step at which this movement should start. Default is a value between 0
    and 10.
    - `step_end` (int, or list of ints, or [MinMaxInt](#MinMaxInt) dict):
    The step at which this movement should end. Overrides `end_after_rotation`.
    Default will use `end_after_rotation`.
    - `rotation_y` (int, or list of ints, or [MinMaxInt](#MinMaxInt)
    dict, or list of MinMaxInt dicts): The amount that the structure
    will rotate each step. Default is randomly either 5 or -5.
    """

    step_begin: RandomizableInt = None
    step_end: RandomizableInt = None
    rotation_y: RandomizableInt = None
    end_after_rotation: RandomizableInt = None


@dataclass
class StructuralTurntableConfig(PositionableStructuralObjectsConfig):
    """
    Defines details of a structural turntable (also sometimes referred to
    as a rotating cog).

    - `num` (int, or list of ints, or [MinMaxInt](#MinMaxInt) dict, or list of
    MinMaxInt dicts): Number of structures to be created with these parameters
    - `labels` (string, or list of strings): A label or labels to be assigned
    to this object. Always automatically assigned "turntables"
    - `material` (string, or list of strings): The structure's material or
    material type. Default: "Custom/Materials/GreyWoodMCS"
    - `position` ([VectorFloatConfig](#VectorFloatConfig) dict, or list of
    VectorFloatConfig dicts): The structure's position in the scene
    - `rotation_y` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat)
    dict, or list of MinMaxFloat dicts): The structure's rotation in the scene.
    Default is 0.
    - `turntable_height` (float, or list of floats, or
    [MinMaxFloat](#MinMaxFloat) dict, or list of MinMaxFloat dicts): Height
    of the turntable/its y-axis scale. Default is 0.1.
    - `turntable_radius` (float, or list of floats, or
    [MinMaxFloat](#MinMaxFloat) dict, or list of MinMaxFloat dicts): Radius
    of the turntable. Will be used to scale the turntable in both x and z
    directions. Default is a value between 0.5 and 1.5.
    - `turntable_movement`
    ([StructuralObjectMovementConfig](#StructuralObjectMovementConfig))
    or list of
    ([StructuralObjectMovementConfig](#StructuralObjectMovementConfig)):
    The config for turntable movement.
    """
    turntable_height: RandomizableFloat = None
    turntable_radius: RandomizableFloat = None
    turntable_movement: Union[StructuralObjectMovementConfig,
                              List[StructuralObjectMovementConfig]] = None


@dataclass
class StructuralTubeOccluderConfig(BaseFeatureConfig):
    """
    Defines details of a structural tube occluder (or "tube-cluder").

    - `num` (int, or list of ints, or [MinMaxInt](#MinMaxInt) dict, or list of
    MinMaxInt dicts): Number of structures to generate with these options.
    - `labels` (string, or list of strings): A label or labels to assign to
    these object(s). Always automatically assigned "tube_occluders"
    - `down_after`: (string, or list of strings): Overrides the `down_step`
    (overriding the manual config) based on the movement of other object(s) in
    the scene. Should be set to one or more labels for mechanical objects that
    may move or rotate, like placers or turntables, as well as agents. The
    `down_step` of this object will be set to the step immediately after ALL of
    the objects and agents finish moving and rotating. If multiple labels are
    configured, all labels will be used. Default: Use `down_step`
    - `down_step` (int, or list of ints, or [MinMaxInt](#MinMaxInt) dict, or
    list of MinMaxInt dicts): The step on which the tube occluder should begin
    to move down from the ceiling to the floor. Note that this should happen
    before `up_step`. Default: between 1 and 10
    - `material` (string, or list of strings): The structure's material or
    material type. Default: random
    - `position_x` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat)
    dict, or list of MinMaxFloat dicts): The structure's X position.
    Default: random
    - `position_z` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat)
    dict, or list of MinMaxFloat dicts): The structure's Z position.
    Default: random
    - `radius` (float, or list of floats, or [MinMaxFloat](#MinMaxFloat)
    dict, or list of MinMaxFloat dicts): The structure's radius. Default:
    between 0.5 and 5
    - `up_after`: (string, or list of strings): Overrides the `up_step`
    (overriding the manual config) based on the movement of other object(s) in
    the scene. Should be set to one or more labels for mechanical objects that
    may move or rotate, like placers or turntables, as well as agents. The
    `up_step` of this object will be set to the step immediately after ALL of
    the objects and agents finish moving and rotating. If multiple labels are
    configured, all labels will be used. Default: Use `up_step`
    - `up_step` (int, or list of ints, or [MinMaxInt](#MinMaxInt) dict, or
    list of MinMaxInt dicts): The step on which the tube occluder should begin
    to move up from the floor to the ceiling. Note that this should happen
    after `down_step`. Default: between 51 and 60
    """
    down_after: RandomizableString = None
    down_step: RandomizableInt = None
    material: RandomizableString = None
    position_x: RandomizableFloat = None
    position_z: RandomizableFloat = None
    radius: RandomizableFloat = None
    up_after: RandomizableString = None
    up_step: RandomizableInt = None


@dataclass
class StructuralNotchedOccluderConfig(BaseFeatureConfig):
    """
    - `height` (float): The overall height of the occluder.
    - `material`(string): The material the occluder is made of.
    - `position_z` (float): The occulders z position within the room.
    - `down_step` (int): The step the occluder will start to move down.
    - `up_step` (int): The step the occluder will start to return up.
    - `notch_height` (float): The height of the occluder notch.
    - `notch_width` (float): The width of the occluder notch.
    """
    height: RandomizableFloat = None
    material: RandomizableString = None
    position_z: RandomizableFloat = None
    down_step: RandomizableInt = None
    up_step: RandomizableInt = None
    notch_height: RandomizableFloat = None
    notch_width: RandomizableFloat = None


DEFAULT_TEMPLATE_DROPPER = StructuralDropperConfig(
    num=0,
    drop_step=MinMaxInt(0, 10),
    projectile_shape=DROPPER_SHAPES,
    position_relative=None
)

DEFAULT_TEMPLATE_THROWER = StructuralThrowerConfig(
    num=0,
    wall=[
        WallSide.FRONT.value,
        WallSide.BACK.value,
        WallSide.LEFT.value,
        WallSide.RIGHT.value],
    impulse=True,
    passive_physics_collision_force=False,
    passive_physics_setup=None,
    passive_physics_throw_force=False,
    throw_step=MinMaxInt(5, 10),
    throw_force=None,
    throw_force_multiplier=None,
    rotation_y=[0, MinMaxInt(-45, 45)],
    rotation_z=[0, MinMaxInt(0, 15)],
    projectile_shape=THROWER_SHAPES,
    position_relative=None,
    stop_position=None,
    path_relative=RelativePathConfig()
)
DEFAULT_THROW_FORCE_IMPULSE = MinMaxInt(5, 20)
DEFAULT_THROW_FORCE_NON_IMPULSE = MinMaxInt(500, 2000)

DEFAULT_TEMPLATE_PARTITION_FLOOR = PartitionFloorConfig(
    leftHalf=0,
    rightHalf=0
)

DEFAULT_TEMPLATE_MOVING_OCCLUDER = StructuralMovingOccluderConfig(
    num=0,
    origin=['top', 'top', 'top', 'top',
            'right', 'left', 'front', 'back'],
    reverse_direction=[True, False],
    occluder_height=MinMaxFloat(
        DEFAULT_MOVING_OCCLUDER_HEIGHT_MIN,
        DEFAULT_MOVING_OCCLUDER_HEIGHT_MAX),
    occluder_thickness=MinMaxFloat(
        DEFAULT_MOVING_OCCLUDER_THICKNESS_MIN,
        DEFAULT_MOVING_OCCLUDER_THICKNESS_MAX),
    occluder_width=MinMaxFloat(
        DEFAULT_MOVING_OCCLUDER_WIDTH_MIN,
        DEFAULT_MOVING_OCCLUDER_WIDTH_MAX),
    rotation_y=MinMaxFloat(
        DEFAULT_OCCLUDER_ROTATION_MIN, DEFAULT_OCCLUDER_ROTATION_MAX),
    move_down_only=False,
    move_up_before_last_step=False,
    repeat_movement=False,
    repeat_interval=MinMaxInt(DEFAULT_MOVING_OCCLUDER_REPEAT_MIN,
                              DEFAULT_MOVING_OCCLUDER_REPEAT_MAX))

ALL_LIPS = StructuralPlatformLipsConfig(True, True, True, True)

DEFAULT_TEMPLATE_PLATFORM = StructuralPlatformConfig(
    num=0,
    lips=StructuralPlatformLipsConfig(False, False, False, False),
    rotation_y=[0, 90, 180, 270],
    position=VectorFloatConfig(None, None, None),
    scale=MinMaxFloat(PLATFORM_SCALE_MIN, PLATFORM_SCALE_MAX),
    attached_ramps=0, platform_underneath=False,
    platform_underneath_attached_ramps=0,
    auto_adjust_platforms=False)

DEFAULT_TEMPLATE_WALL = StructuralWallConfig(
    num=0,
    position=VectorFloatConfig(None, None, None),
    rotation_y=[0, 90, 180, 270],
    width=None,
    thickness=None,
    height=None,
    ignore_bounds=False,
    same_material_as_room=False
)

DEFAULT_TEMPLATE_RAMP = StructuralRampConfig(
    num=0, position=VectorFloatConfig(None, None, None),
    rotation_y=[0, 90, 180, 270],
    angle=MinMaxFloat(RAMP_ANGLE_MIN, RAMP_ANGLE_MAX),
    platform_underneath=False, platform_underneath_attached_ramps=0)

DEFAULT_TEMPLATE_L_OCCLUDER = StructuralLOccluderConfig(
    num=0, position=VectorFloatConfig(None, None, None),
    rotation_y=[0, 90, 180, 270],
    scale_front_x=MinMaxFloat(L_OCCLUDER_SCALE_MIN, L_OCCLUDER_SCALE_MAX),
    scale_front_z=MinMaxFloat(L_OCCLUDER_SCALE_MIN, L_OCCLUDER_SCALE_MAX),
    scale_side_x=MinMaxFloat(L_OCCLUDER_SCALE_MIN, L_OCCLUDER_SCALE_MAX),
    scale_side_z=MinMaxFloat(L_OCCLUDER_SCALE_MIN, L_OCCLUDER_SCALE_MAX),
    scale_y=MinMaxFloat(L_OCCLUDER_SCALE_MIN, L_OCCLUDER_SCALE_MAX),
    backwards=[True, False])

DEFAULT_TEMPLATE_HOLES_LAVA = FloorAreaConfig()

DEFAULT_TEMPLATE_FLOOR_MATERIALS = FloorMaterialConfig()

DEFAULT_TEMPLATE_OCCLUDING_WALL = StructuralOccludingWallConfig(
    num=0,
    type=['occludes', 'occludes', 'occludes', 'short', 'thin', 'hole'],
    keyword_location=None,
    scale=None, rotation_y=MinMaxFloat(
        DEFAULT_OCCLUDER_ROTATION_MIN, DEFAULT_OCCLUDER_ROTATION_MAX),
    material=[mat[0] for mat in materials.ROOM_WALL_MATERIALS],
    position=VectorFloatConfig(None, None, None))

DEFAULT_TEMPLATE_PLACER = StructuralPlacerConfig(
    0,
    placed_object_above=None,
    placed_object_position=None,
    placed_object_rotation=MinMaxFloat(0, 359),
    placed_object_scale=None,
    placed_object_shape=PLACER_SHAPES,
    activation_step=MinMaxInt(1, 10),
    activate_after=None,
    activate_on_start_or_after=None,
    deactivation_step=None,
    end_height=0,
    position_relative=None,
    empty_placer=False,
    pickup_object=False,
    retain_position=False,
    move_object=False,
    move_object_end_position=None,
    move_object_y=None,
    move_object_z=None,
    existing_object_required=False
)

DOOR_MATERIAL_RESTRICTIONS = [mat[0] for mat in (materials.METAL_MATERIALS +
                                                 materials.PLASTIC_MATERIALS +
                                                 materials.WOOD_MATERIALS)]

DEFAULT_TEMPLATE_DOOR = StructuralDoorConfig(
    num=0, position=VectorFloatConfig(None, None, None),
    rotation_y=[0, 90, 180, 270],
    material=DOOR_MATERIAL_RESTRICTIONS,
    wall_material=materials.ROOM_WALL_MATERIALS,
    wall_scale_x=None, wall_scale_y=None)

DEFAULT_TEMPLATE_TUBE_OCCLUDER = StructuralTubeOccluderConfig(
    num=0,
    down_after=None,
    down_step=MinMaxInt(1, 10),
    material=None,
    position_x=None,
    position_z=None,
    radius=MinMaxFloat(0.5, 5),
    up_after=None,
    up_step=MinMaxInt(51, 60)
)

DEFAULT_TEMPLATE_STRUCT_OBJ_MOVEMENT = StructuralObjectMovementConfig(
    step_begin=MinMaxInt(0, 10), step_end=None,
    rotation_y=[-5, 5], end_after_rotation=[90, 180, 270, 360]
)
DEFAULT_TURNTABLE_MATERIAL = MaterialTuple(
    'Custom/Materials/GreyWoodMCS',
    ['grey']
)
DEFAULT_TEMPLATE_TURNTABLE = StructuralTurntableConfig(
    num=0, position=VectorFloatConfig(x=None, y=0, z=None),
    rotation_y=0,
    material=DEFAULT_TURNTABLE_MATERIAL.material,
    turntable_height=DEFAULT_TURNTABLE_HEIGHT,
    turntable_radius=MinMaxFloat(
        DEFAULT_TURNTABLE_MIN_RADIUS,
        DEFAULT_TURNTABLE_MAX_RADIUS
    ),
    turntable_movement=DEFAULT_TEMPLATE_STRUCT_OBJ_MOVEMENT
)

DEFAULT_TEMPLATE_NOTCHED_OCCLUDER = StructuralNotchedOccluderConfig(
    height=MinMaxFloat(2.1, 3),
    material=None,
    position_z=MinMaxFloat(1, 3.0),
    down_step=MinMaxInt(1, 10),
    up_step=MinMaxInt(51, 60),
    notch_height=MinMaxFloat(1, 2),
    notch_width=MinMaxFloat(0.3, 2.1)
)


# This is structural only
def _post_instance(scene, new_obj, template, source_template, type):
    add_random_placement_tag(new_obj, source_template)
    plat_under = getattr(template, 'platform_underneath', None)
    new_objs = new_obj if isinstance(new_obj, list) else [new_obj]
    new_obj = new_objs[0]
    for obj in new_objs:
        extra_labels = (
            source_template.labels
            if source_template and source_template.labels
            else [])
        extra_labels = extra_labels if isinstance(
            extra_labels, list) else [extra_labels]
        obj['debug']['labels'] = (
            [type.lower()] + (extra_labels))
    if plat_under or getattr(template, 'attached_ramps', None) or getattr(
            template, 'long_with_two_ramps', None):
        if (new_obj and new_obj["debug"]["labels"] is not None):
            new_obj["debug"]["labels"].append(LABEL_CONNECTED_TO_RAMP)
        else:
            new_obj["debug"]["labels"] = [LABEL_CONNECTED_TO_RAMP]
        # if we ever try to attach to l_occluders, this won't work
        start_scene = copy.deepcopy(scene)
        last_exception = None
        for _ in range(MAX_TRIES):
            try:
                new_objs = _add_platform_attached_objects(
                    scene, template, new_obj)
                return new_objs
            except ILEException as e:
                scene = copy.deepcopy(start_scene)
                last_exception = e
                continue
        raise ILEException(
            f'Failed adding object attached to platform={new_obj}'
        ) from last_exception
    return new_objs


def _handle_position_defaults(
    scene: Scene,
    reconciled: BaseFeatureConfig
) -> BaseFeatureConfig:
    """Convenience method for many of the structural objects"""
    room_dim = scene.room_dimensions or {}
    def_dim = geometry.DEFAULT_ROOM_DIMENSIONS
    room_width = room_dim.x or def_dim['x']
    room_length = room_dim.z or def_dim['z']

    reconciled.position = (
        VectorFloatConfig(None, None, None)
        if reconciled.position is None else reconciled.position)
    reconciled.position.x = (
        MinMaxFloat(-room_width / 2.0, room_width / 2.0).convert_value()
        if reconciled.position.x is None else reconciled.position.x
    )
    reconciled.position.z = (
        MinMaxFloat(-room_length / 2.0, room_length / 2.0).convert_value()
        if reconciled.position.z is None else reconciled.position.z
    )
    reconciled.rot = (geometry.random_rotation()
                      if reconciled.rotation_y is None
                      else reconciled.rotation_y)

    plat_under = getattr(reconciled, 'platform_underneath', None)
    if reconciled.position.y == 0 and plat_under:
        # this assumes we never want to put these structures in holes
        raise ILEException("Cannot put platform underneath structural "
                           "object with y position less than 0")

    room_height = room_dim.y or def_dim['y']
    if reconciled.position.y is None:
        if plat_under:
            # what should the default be?
            reconciled.position.y = MinMaxFloat(
                TOP_PLATFORM_POSITION_MIN,
                room_height - geometry.PERFORMER_HEIGHT).convert_value()
        else:
            reconciled.position.y = 0
    return reconciled


def _create_contiguous_floor_features(
    scene: Scene,
    starting_x: float,
    starting_z: float,
    size: int,
    bounds_list: List[ObjectBounds],
    scene_attribute: str
) -> List[Vector2dInt]:
    """Create and return a list of contiguous floor features."""
    area_list = [Vector2dInt(x=starting_x, z=starting_z)]
    valid, _ = validate_floor_position(
        scene,
        area_list[0],
        scene_attribute,
        True,
        bounds_list
    )
    if not valid:
        raise ILEException(
            f'Starting {scene_attribute} floor feature has been assigned an '
            f'invalid position: x={starting_x}, z={starting_z}'
        )

    for _ in range((size or 1) - 1):
        area = None
        adjacent_options = area_list.copy()
        random.shuffle(adjacent_options)
        for adjacent_option in adjacent_options:
            area_options = [
                Vector2dInt(x=(adjacent_option.x - 1), z=adjacent_option.z),
                Vector2dInt(x=(adjacent_option.x + 1), z=adjacent_option.z),
                Vector2dInt(x=adjacent_option.x, z=(adjacent_option.z - 1)),
                Vector2dInt(x=adjacent_option.x, z=(adjacent_option.z + 1))
            ]
            random.shuffle(area_options)
            for area_option in area_options:
                if area_option in area_list:
                    continue
                valid, _ = validate_floor_position(
                    scene,
                    area_option,
                    scene_attribute,
                    True,
                    bounds_list
                )
                if valid:
                    area = area_option
                    break
            if area:
                break
        if not area:
            raise ILEException(
                f'Cannot find any valid {scene_attribute} position adjacent '
                f'to : {[(vector.x, vector.z) for vector in area_list]}'
            )
        area_list.append(area)

    return area_list


def _is_valid_floor(
    scene: Scene,
    position_list: List[Vector2dInt],
    scene_attribute: str,
    restrict_under_user: bool,
    bounds_list: List[ObjectBounds],
    try_num: int,
    retries: int,
    feature_type_string: str
) -> bool:
    """Return whether the given floor features are valid (they do not collide
    with other objects in the scene, or the performer agent). If all are valid,
    then this function will add their bounds to the given bounds_list."""
    invalid = False
    new_bounds_list = []
    for position in position_list:
        valid, bounds = validate_floor_position(
            scene,
            position,
            scene_attribute,
            restrict_under_user,
            bounds_list
        )
        if not valid:
            invalid = True
            break
        new_bounds_list.append(bounds)

    if invalid:
        # Checks if enabled for TRACE logging.
        if logger.isEnabledFor(logging.TRACE):
            logger.trace(
                f'Failed validating location of {feature_type_string} on'
                f' try {try_num + 1} of {retries}.'
                f'\nFAILED FLOOR POSITION = {position}'
            )
        else:
            logger.debug(
                f'Failed validating location of {feature_type_string} on'
                f' try {try_num + 1} of {retries}.'
            )
        return False

    for bounds in new_bounds_list:
        bounds_list.append(bounds)
    return True


def _add_floor_dependent_defaults(scene, reconciled):
    """Convenience method for floor features"""
    room_dim = scene.room_dimensions
    xmax = math.floor(room_dim.x / 2)
    zmax = math.floor(room_dim.z / 2)
    reconciled.position_x = (
        reconciled.position_x if reconciled.position_x is not None
        else random.randint(-xmax, xmax))
    reconciled.position_z = (
        reconciled.position_z if reconciled.position_z is not None
        else random.randint(-zmax, zmax)
    )
    reconciled.size = max(reconciled.size or 1, 1)


def _add_platform_attached_objects(
        scene,
        orig_template: Union[StructuralPlatformConfig,
                             StructuralRampConfig],
        obj: dict):
    # Ideally, we've position everything and then rotate everything around a
    # single axis.  Doing this is harder with some of the utlity functions
    # available and can limit retries since objects may be moved outside the
    # room late.  Therefore, we create and place each additional object and
    # then rotate it around the original objects position.  To create some
    # objects, we need information from objects from a pre-rotation state.
    # We simply return and store these states when needed.

    # bounds to test ramps to make sure they don't hit each other.  We'll
    # check all objects against all other objects when finished.
    local_bounds = []
    objs = [obj]
    new_platform = None
    top_pos = rotation_point = obj['shows'][0]['position']
    top_scale = obj['shows'][0]['scale']
    mat = obj['materials'][0]
    rotation_y = obj['shows'][0]['rotation']['y']
    below_pre_rot_pos = None

    attached_ramps = getattr(orig_template, 'attached_ramps', None)
    long_with_two_ramps = getattr(orig_template, 'long_with_two_ramps', None)
    if getattr(orig_template, 'platform_underneath', None):
        new_platform, below_pre_rot_pos = _add_platform_below(
            scene, obj, rotation_point, orig_template)
        objs += [new_platform]
        below_scale = new_platform['shows'][0]['scale']
        if orig_template.adjacent_to_wall:
            new_platform['debug']['adjacent_to_wall'] = \
                orig_template.adjacent_to_wall
    if attached_ramps or long_with_two_ramps:
        if below_pre_rot_pos:
            available_lengths = _get_space_around_platform(
                top_pos=top_pos,
                top_scale=top_scale,
                bottom_pos=below_pre_rot_pos,
                bottom_scale=below_scale
            )
        else:
            available_lengths = _get_space_around_platform(
                top_pos=top_pos,
                top_scale=top_scale,
                bottom_pos={'x': 0, 'y': 0, 'z': 0},
                bottom_scale=scene.room_dimensions.dict()
            ) if int(rotation_y) % 90 == 0 else DEFAULT_AVAILABLE_LENGTHS

        # Attach ramps
        gaps = []
        ramps_to_add = 0
        short_scales = None
        if attached_ramps:
            ramps_to_add += attached_ramps
        if long_with_two_ramps:
            ramps_to_add = 2
            scale = obj['shows'][0]['scale']
            short_scales = \
                ['x-', 'x+'] if scale['x'] < scale['z'] else ['z-', 'z+']

        for i in range(ramps_to_add):
            logger.trace(
                f"Attempting to attach ramp {i}/"
                f"{orig_template.attached_ramps} to platform.")
            gap = _add_valid_ramp(
                scene, objs, bounds=local_bounds,
                pre_rot_pos=top_pos,
                scale=top_scale,
                rotation_y=rotation_y,
                rotation_point=rotation_point,
                material=mat,
                max_angle=45,
                available_lengths=available_lengths,
                short_scale_long_with_two_ramps=(None if not short_scales else short_scales[i])  # noqa
            )
            gaps.append(gap)
        _add_gaps_to_object(gaps, obj)

    if (getattr(orig_template, 'platform_underneath_attached_ramps',
                None) or long_with_two_ramps) and new_platform:
        gaps = []
        ramps_to_add = 0
        short_scales = None
        if orig_template.platform_underneath_attached_ramps:
            ramps_to_add += orig_template.platform_underneath_attached_ramps
        if long_with_two_ramps:
            ramps_to_add = 2
            scale = obj['shows'][0]['scale']
            short_scales = \
                ['x-', 'x+'] if scale['x'] < scale['z'] else ['z-', 'z+']
        available_lengths = _get_space_around_platform(
            top_pos=below_pre_rot_pos,
            top_scale=below_scale,
            bottom_pos={'x': 0, 'y': 0, 'z': 0},
            bottom_scale=scene.room_dimensions.dict()
        ) if int(rotation_y) % 90 == 0 else DEFAULT_AVAILABLE_LENGTHS
        for i in range(ramps_to_add):
            logger.trace(
                f"Attempting to attach ramp {i}/"
                f"{orig_template.platform_underneath_attached_ramps} to "
                f"underneath platform.")
            new_mat = new_platform['materials'][0]
            gap = _add_valid_ramp(
                scene, objs, bounds=local_bounds,
                pre_rot_pos=below_pre_rot_pos,
                scale=below_scale,
                rotation_y=rotation_y,
                rotation_point=rotation_point,
                material=new_mat,
                max_angle=45,
                available_lengths=available_lengths,
                short_scale_long_with_two_ramps=(None if not short_scales else short_scales[i])  # noqa
            )
            gaps.append(gap)
        _add_gaps_to_object(gaps, new_platform)
    return objs


def _add_gaps_to_object(gaps_list, obj):
    all_gaps = {}
    lips_gaps = {}
    lips_cfg = obj['lips']

    for gap in gaps_list:
        side = gap['side']
        _add_gaps_to_dict(all_gaps, side, gap)
        if not lips_cfg[side]:
            continue
        _add_gaps_to_dict(lips_gaps, side, gap)
    if all_gaps:
        obj['debug']['gaps'] = all_gaps
    if lips_gaps:
        obj['lips']['gaps'] = lips_gaps


def _add_gaps_to_dict(gaps_dict, side, gap_to_copy):
    gaps = gaps_dict.get(side, [])
    gap = copy.deepcopy(gap_to_copy)
    gap.pop('side')
    gaps.append(gap)
    gaps = sorted(gaps, key=lambda item: item['low'])
    gaps_dict[side] = gaps


def _calculate_adjacent_to_wall_pos(
        room_dim_x, room_dim_z, adjacent_to_wall,
        pos_x, pos_z, scale_x, scale_z):
    if isinstance(adjacent_to_wall, list):
        adjacent_to_wall = adjacent_to_wall[0]
    x_side = (
        1 if 'right' in adjacent_to_wall else
        (-1) if 'left' in adjacent_to_wall else None)
    z_side = (
        1 if 'front' in adjacent_to_wall else
        (-1) if 'back' in adjacent_to_wall else None)
    if x_side:
        pos_x = (room_dim_x / 2 * x_side) + (scale_x / 2 * -x_side)
    if z_side:
        pos_z = (room_dim_z / 2 * z_side) + (scale_z / 2 * -z_side)
    return pos_x, pos_z


def _add_platform_below(scene, obj, rotation_point, top_template):
    show = obj['shows'][0]
    scale = show['scale']
    pos = show['position']
    min_y = show['boundingBox'].min_y
    for _ in range(MAX_TRIES):
        # some assumptions:
        # rotation between platforms is fixed

        max_room_dim = max(
            scene.room_dimensions.x,
            scene.room_dimensions.z)
        x0, z0, scale_x, scale_z = _get_under_platform_position_scale(
            scale, pos, max_room_dim, top_template.long_with_two_ramps)

        if top_template.adjacent_to_wall:
            x0, z0 = _calculate_adjacent_to_wall_pos(
                scene.room_dimensions.x, scene.room_dimensions.z,
                top_template.adjacent_to_wall, x0, z0, scale_x, scale_z)

        # rotation around arbitrary center is:
        # x1 = (x0 -xc) cos(theta) - (z0 -zc)sin(theta) + xc
        # z1 = (x0 -xc) sin(theat) + (z0 -zc)cos(theta) + zc
        # rotate position around obj position
        r_point_x = rotation_point['x']
        r_point_z = rotation_point['z']
        radians = math.radians(obj['shows'][0]['rotation']['y'])
        x = (x0 - r_point_x) * math.cos(radians) - \
            (z0 - r_point_z) * math.sin(radians) + r_point_x
        z = -(x0 - r_point_x) * math.sin(radians) - \
            (z0 - r_point_z) * math.cos(radians) + r_point_z

        labels_to_use = list()
        labels_to_use.append(LABEL_CONNECTED_TO_RAMP)

        if (top_template.labels is not None):
            if (isinstance(top_template.labels, str)):
                labels_to_use.append(top_template.labels)
            else:
                labels_to_use.extend(top_template.labels)

        new_template = StructuralPlatformConfig(
            num=1,
            material=[
                material_tuple.material
                for material_tuple in materials.ROOM_WALL_MATERIALS
                if material_tuple.material != obj['materials'][0]
            ],
            position=VectorFloatConfig(x, 0, z),
            rotation_y=show['rotation']['y'],
            scale=VectorFloatConfig(
                scale_x,
                min_y,
                scale_z),
            lips=(top_template.lips if not
                  top_template.long_with_two_ramps else ALL_LIPS),
            labels=labels_to_use)
        srv = StructuralPlatformCreationService()
        reconciled = srv.reconcile(scene, new_template)
        new_platform = srv.create_feature_from_specific_values(
            scene, reconciled, new_template)
        new_platform = (new_platform[0] if isinstance(new_platform, List)
                        else new_platform)
        new_platform['debug']['random_position'] = True
        return new_platform, {'x': x0, 'y': min_y * 0.5, 'z': z0}

    raise ILEException("Failed to add platform under existing platform.")


def _get_under_platform_position_scale(
        top_scale, top_position, max_room_dim, long_with_two_ramps=None):
    # How do we want to determine the position and scale of the platform below?
    # This has a range which is determined based on the top objects scale.
    # We then choose a random position where the top object entirely fits on
    # the bottom platform.

    # top_scale['y'] is the ramp length at 45 degrees (max angle)
    scale_min_buffer = max(
        top_scale['y'] +
        BOTTOM_PLATFORM_SCALE_BUFFER_MIN,
        top_scale['y'] * 2)
    scale_max_buffer = min(
        max_room_dim - geometry.PERFORMER_WIDTH -
        min(top_scale['x'], top_scale['z']),
        top_scale['y'] +
        BOTTOM_PLATFORM_SCALE_BUFFER_MAX)
    scale_x = (
        top_scale['x'] if long_with_two_ramps and
        top_scale['x'] > top_scale['z'] else
        MinMaxFloat(
            top_scale['x'] + scale_min_buffer,
            top_scale['x'] + scale_max_buffer
        ).convert_value())
    scale_z = (
        top_scale['z'] if long_with_two_ramps and
        top_scale['z'] > top_scale['x'] else
        MinMaxFloat(
            top_scale['z'] + scale_min_buffer,
            top_scale['z'] + scale_max_buffer
        ).convert_value())

    top_pos_x = _get_pre_rotate_under_position(
        top_scale, top_position, scale_x, 'x')
    top_pos_z = _get_pre_rotate_under_position(
        top_scale, top_position, scale_z, 'z')

    if long_with_two_ramps:
        top_pos_x = top_position['x']
        top_pos_z = top_position['z']

    return top_pos_x, top_pos_z, scale_x, scale_z


def _get_pre_rotate_under_position(
        top_scale, top_position, bot_scale, key):
    """determine the range of positions in one dimension for a platform under
    another platform or ramp to ensure the top (original) object is
    entirely contained in the bottom in this one dimension.
    """
    pos_min = top_position[key] + top_scale[key] * 0.5 - bot_scale * 0.5
    pos_max = top_position[key] - top_scale[key] * 0.5 + bot_scale * 0.5
    return random.uniform(pos_min, pos_max)


def _add_valid_ramp(
        scene, objs, bounds,
        pre_rot_pos, scale, rotation_y,
        rotation_point, material,
        max_angle, available_lengths,
        short_scale_long_with_two_ramps=None):
    """ Returns gap location"""
    ramp, gap = _get_attached_ramp(
        scene,
        pre_rot_pos=pre_rot_pos, scale=scale,
        rotation_y=rotation_y,
        rotation_point=rotation_point,
        material=material,
        available_lengths=available_lengths,
        max_angle=max_angle,
        short_scale_long_with_two_ramps=short_scale_long_with_two_ramps)
    if validate_all_locations_and_update_bounds([ramp], scene, bounds):
        objs.append(ramp)
        return gap
    raise ILEException("Unable to find valid location to attach ramp to "
                       "platform.  This is usually due too many ramps for the"
                       "amount of space.")


def _get_attached_ramp(scene: Scene, pre_rot_pos: dict, scale: dict,
                       rotation_y, rotation_point, material, available_lengths,
                       max_angle=89, short_scale_long_with_two_ramps=None
                       ):
    ppx = pre_rot_pos['x']
    ppy = pre_rot_pos['y']
    ppz = pre_rot_pos['z']
    psx = scale['x']
    psy = scale['y']
    psz = scale['z']

    r_point_x = rotation_point['x']
    r_point_z = rotation_point['z']

    performer_buffer = geometry.PERFORMER_HALF_WIDTH * 2
    # then randomize which edge we choose first

    if not short_scale_long_with_two_ramps:
        edge_choices = [0, 1, 2, 3]  # left, front, right, back
        random.shuffle(edge_choices)
    else:
        edge_choices = (
            [0] if short_scale_long_with_two_ramps == 'x-' else [2] if
            short_scale_long_with_two_ramps == 'x+' else
            [3] if short_scale_long_with_two_ramps == 'z-' else [1])

    # get how much room we have for a ramp in this edge if on top of
    # another platform
    # default to a high number.  Ignore the space check if platform is on
    # floor.

    valid_edge = False
    # verify there is enough space for a ramp
    for edge in edge_choices:
        max_ramp_length = min(
            available_lengths[edge] -
            performer_buffer,
            ATTACHED_RAMP_MAX_LENGTH)
        if max_ramp_length <= 0:
            continue
        min_ramp_length = round(psy / math.tan(math.radians(max_angle)), 4)
        min_ramp_length = max(min_ramp_length, ATTACHED_RAMP_MIN_LENGTH)

        # what angle do we need to get to the necessary height given the max
        # length.
        angle_needed = math.degrees(math.atan(psy / (max_ramp_length)))
        # make sure ramp isn't wider than platform
        ramp_width_max = min(
            ATTACHED_RAMP_MAX_WIDTH,
            psz if edge %
            2 == 0 else psx)
        if ((angle_needed <= max_angle and angle_needed >=
                0) and (max_ramp_length > min_ramp_length) and
                (ramp_width_max >= ATTACHED_RAMP_MIN_WIDTH)):
            scale = VectorFloatConfig(
                MinMaxFloat(ATTACHED_RAMP_MIN_WIDTH, ramp_width_max),
                psy,
                MinMaxFloat(min_ramp_length, max_ramp_length))
            scale = choose_random(scale)
            valid_edge = True
            break

    if not valid_edge:
        raise ILEException(
            f"Unable to add ramp to given platform with angle less than "
            f"{max_angle}")

    rsx = scale.x
    rsz = scale.z
    rot = rotation_y

    # need to rotate ramps when all is done so they go from the platform down.
    rot_add = RAMP_ROTATIONS[edge]

    rpy = ppy - 0.5 * psy

    length = rsz
    r_angle = math.degrees(math.atan(psy / rsz))

    # determing the position of the ramp is somewhat complicated.
    # The steps are:
    #  Determine position as if there is no rotation.
    #  Rotate the position around the center (position) of the platform

    # x limit when x of ramp is outside the platform relative to platform
    # origin
    x_limit_out = psx * 0.5 + 0.5 * rsz
    # x limit when x of ramp is inside the platform relative to platform origin
    x_limit_in = psx * 0.5 - 0.5 * rsx
    # same with z
    z_limit_out = psz * 0.5 + 0.5 * rsz
    z_limit_in = psz * 0.5 - 0.5 * rsx

    # position ranges as if platform has no rotation.  Each
    # index corresponds to one edge of the platform.
    non_rot_x_min = [-x_limit_out, -x_limit_in, x_limit_out, -x_limit_in]
    non_rot_x_max = [-x_limit_out, x_limit_in, x_limit_out, x_limit_in]
    non_rot_z_min = [-z_limit_in, -z_limit_out, -z_limit_in, z_limit_out]
    non_rot_z_max = [z_limit_in, -z_limit_out, z_limit_in, z_limit_out]

    # rotation around arbitrary center is:
    # x1 = (x0 -xc) cos(theta) - (z0 -zc)sin(theta) + xc
    # z1 = (x0 -xc) sin(theat) + (z0 -zc)cos(theta) + zc
    rel_x0 = random.uniform(non_rot_x_min[edge], non_rot_x_max[edge])
    rel_z0 = random.uniform(non_rot_z_min[edge], non_rot_z_max[edge])

    if edge % 2 == 0:
        # on left or right, we need to determine range in z
        # ramp width is always x so always use rsx
        gap = _get_lip_gap(rel_z0, rsx, ppz, psz, edge)
    else:
        # on front or back, we need to determine range in x
        gap = _get_lip_gap(rel_x0, rsx, ppx, psx, edge)

    x0 = rel_x0 + ppx
    z0 = rel_z0 + ppz

    radians = math.radians(rot)
    rpx = (x0 - r_point_x) * math.cos(radians) - \
        (z0 - r_point_z) * math.sin(radians) + r_point_x
    rpz = -(x0 - r_point_x) * math.sin(radians) - \
        (z0 - r_point_z) * math.cos(radians) + r_point_z

    pos = VectorFloatConfig(rpx, rpy, rpz)

    rot = (rot + rot_add) % 360
    new_template = StructuralRampConfig(
        num=1, labels=LABEL_BIDIRECTIONAL_RAMP,
        position=pos, rotation_y=rot, angle=r_angle,
        length=length, width=rsx, material=material)
    svc = StructuralRampCreationService()
    reconciled = svc.reconcile(scene, new_template)
    new_ramp = (svc.create_feature_from_specific_values(
        scene, reconciled, new_template))
    new_ramp = (new_ramp[0] if isinstance(new_ramp, List)
                else new_ramp)
    new_ramp['debug']['random_position'] = True
    return new_ramp, gap


def _get_lip_gap(ramp_pos, ramp_scale, plat_pos, plat_scale, edge):
    # ramp_pos is relative to the platform.  To get the actual position, we
    # would add plat_pos as we do in the function where this is called.
    # However that should get factored out so we don't need it.
    # edge 0 = -x left, 1=-z back, 2=+x right, 3=+z front
    gap = {'side': ['left', 'back', 'right', 'front'][edge]}
    gap['high'] = (ramp_pos + ramp_scale * 0.5 +
                   plat_scale * 0.5) / plat_scale
    gap['low'] = (ramp_pos - ramp_scale * 0.5 + plat_scale * 0.5) / plat_scale
    # I'm not sure why I need to reverse the direction here.
    if edge % 2 == 0:
        temp = 1 - gap['high']
        gap['high'] = 1 - gap['low']
        gap['low'] = temp
    return gap


def _get_space_around_platform(
        top_pos: dict, top_scale: dict, bottom_pos: dict, bottom_scale: dict):
    """All values must be pre-rotation and both objects must have the same
    rotation applied.
    """
    # delta pos
    dposx = bottom_pos['x'] - top_pos['x']
    dposz = bottom_pos['z'] - top_pos['z']
    # delta scale
    dscalex = bottom_scale['x'] - top_scale['x']
    dscalez = bottom_scale['z'] - top_scale['z']

    x_positive = dposx + dscalex / 2
    x_negative = -dposx + dscalex / 2

    z_positive = dposz + dscalez / 2
    z_negative = -dposz + dscalez / 2
    logger.debug(f" Area around platform for ramps: "
                 f"{[x_negative, z_negative, x_positive, z_positive]}")
    return [x_negative, z_negative, x_positive, z_positive]


def _get_occluding_wall_scale(
    template: StructuralOccludingWallConfig,
    target: InstanceDefinitionLocationTuple,
    room_height: float
) -> tuple(Vector3d, Vector3d):
    scale = template.scale
    if scale is None:
        scale = Vector3d()
    if isinstance(scale, (int, float)):
        scale = Vector3d(x=scale, y=scale, z=scale)

    dim = None
    if target:
        try:
            # Try and fall back to definition
            temp_dim = target.instance['debug']['dimensions']
            dim = Vector3d(x=temp_dim['x'], y=temp_dim['y'], z=temp_dim['z'])
        except Exception:
            dim = target.definition.dimensions

    if not scale.x:
        if template.type == OccludingWallType.THIN:
            scale.x = (min(dim.x, dim.z) * random.uniform(
                DEFAULT_OCCLUDING_WALL_SHORT_THIN_SCALE_MIN,
                DEFAULT_OCCLUDING_WALL_SHORT_THIN_SCALE_MAX
            )) if dim else DEFAULT_OCCLUDING_WALL_SHORT_THIN_SCALE_MIN
        elif template.type == OccludingWallType.HOLE:
            scale.x = (
                max(dim.x, dim.z) + OCCLUDING_WALL_WIDTH_BUFFER
            ) if dim else choose_random(DEFAULT_OCCLUDING_WALL_WIDTH)
        else:
            scale.x = (
                max(dim.x, dim.z) * DEFAULT_OCCLUDING_WALL_WIDTH_MULTIPLIER
            ) if dim else choose_random(DEFAULT_OCCLUDING_WALL_WIDTH)

    if not scale.y:
        if template.type == OccludingWallType.SHORT:
            scale.y = (dim.y * random.uniform(
                DEFAULT_OCCLUDING_WALL_SHORT_THIN_SCALE_MIN,
                DEFAULT_OCCLUDING_WALL_SHORT_THIN_SCALE_MAX
            )) if dim else DEFAULT_OCCLUDING_WALL_SHORT_THIN_SCALE_MIN
            scale.y = max(scale.y, DEFAULT_OCCLUDING_WALL_SHORT_SCALE_Y_MIN)
        elif template.type == OccludingWallType.HOLE:
            scale.y = (
                dim.y + min(OCCLUDING_WALL_HOLE_MAX_HEIGHT, dim.y)
            ) if dim else choose_random(DEFAULT_OCCLUDING_WALL_HEIGHT)
        else:
            scale.y = (
                dim.y * DEFAULT_OCCLUDING_WALL_HEIGHT_MULTIPLIER
            ) if dim else choose_random(DEFAULT_OCCLUDING_WALL_HEIGHT)

    if not scale.z:
        scale.z = choose_random(DEFAULT_OCCLUDING_WALL_THICKNESS)

    # Restrict max height to room height.
    scale.y = min(scale.y, room_height)
    return (scale, dim)


def _get_occluding_wall_definition(
        template: StructuralOccludingWallConfig) -> ObjectDefinition:
    return ObjectDefinition(
        type='cube',
        attributes=['structure', 'kinematic'],
        color=template.material[1],
        scale=template.scale,
        dimensions=template.scale,
        materials=[template.material],
        materialCategory=[],
        salientMaterials=[],
        shape=['cube'],
        size='huge'
    )


def _get_room_min_max_dimensions(scene):
    room_dim = scene.room_dimensions or {}
    def_dim = geometry.DEFAULT_ROOM_DIMENSIONS
    room_width = room_dim.x or def_dim['x']
    room_length = room_dim.z or def_dim['z']

    min_room_dim = min(room_width, room_length)
    max_room_dim = max(room_width, room_length)
    return min_room_dim, max_room_dim


def _reconcile_material(
    material_choice: Union[MaterialTuple, str, List[str]],
    default_materials: List[MaterialTuple],
    prohibited_material: str = None
) -> MaterialTuple:
    # If the material_choice is a MaterialTuple, return it.
    if isinstance(material_choice, MaterialTuple):
        return material_choice
    output = None
    # If the material_choice is a string, convert it to a MaterialTuple.
    # If a string list, choose a random string, and then convert it.
    # Ensure it is not the prohibited_material.
    if material_choice:
        output = choose_material_tuple_from_material(
            material_choice,
            prohibited_material
        )
    # Otherwise randomly choose from the other given materials.
    if not output:
        output = choose_material_tuple_from_material(
            [item.material for item in default_materials],
            prohibited_material
        )
    return output


def _modify_for_hole(type, base, target_dimensions):
    show = base['shows'][0]
    if type == OccludingWallType.HOLE:
        # boost position by half of each section.  each section is half
        # the height
        show['position']['y'] = show['scale']['y'] / 4.0
        # copy instance and create new instances.
        l_col, r_col, top = _convert_base_occluding_wall_to_holed_wall(
            base, target_dimensions)
        walls = structures.finalize_structural_object(
            [l_col, r_col, top])
        return walls
    else:
        show['position']['y'] = show['scale']['y'] / 2.0
        obj = structures.finalize_structural_object([base])[0]
        return [obj]


def _convert_base_occluding_wall_to_holed_wall(
        base: SceneObject, target_dim: Vector3d):
    l_col = copy.deepcopy(base)
    r_col = copy.deepcopy(base)
    top = copy.deepcopy(base)

    rot = base['shows'][0]['rotation']['y']
    l_pos = l_col['shows'][0]['position']
    l_scale = l_col['shows'][0]['scale']
    r_pos = r_col['shows'][0]['position']
    r_scale = r_col['shows'][0]['scale']
    t_pos = top['shows'][0]['position']
    t_scale = top['shows'][0]['scale']

    if t_scale['y'] / 2 > OCCLUDING_WALL_HOLE_MAX_HEIGHT:
        hole_height = OCCLUDING_WALL_HOLE_MAX_HEIGHT
    else:
        hole_height = t_scale['y'] / 2

    l_scale['x'] = OCCLUDING_WALL_WIDTH_BUFFER / 2.0
    r_scale['x'] = OCCLUDING_WALL_WIDTH_BUFFER / 2.0
    l_scale['y'] = hole_height
    r_scale['y'] = hole_height
    t_scale['y'] -= hole_height
    r_pos['y'] = hole_height / 2.0
    l_pos['y'] = hole_height / 2.0
    t_pos['y'] = hole_height + (t_scale['y'] / 2.0)
    sin = math.sin(math.radians(rot))
    cos = math.cos(math.radians(rot))
    shift = t_scale['x'] - l_scale['x']
    shift /= 2.0

    l_pos['x'] -= shift * cos
    l_pos['z'] += shift * sin
    r_pos['x'] += shift * cos
    r_pos['z'] -= shift * sin

    # scale x/3, y/2, z, shifted -x/3 (note rotation)
    # scale x/3, y/2, z, shifted -x/3 (note rotation)
    # scale x, y/2, z, shifted y
    l_col['id'] = f"l_col-{l_col['id']}"
    r_col['id'] = f"r_col-{r_col['id']}"
    top['id'] = f"top-{top['id']}"
    return l_col, r_col, top


def _get_existing_held_object_idl(
    labels: RandomizableString,
    place_or_pickup: bool = False,
    existing_object_required: bool = False
) -> Optional[InstanceDefinitionLocationTuple]:
    object_repository = ObjectRepository.get_instance()
    shuffled_labels = (
        labels if isinstance(labels, list) else [labels]
    ) if labels else []
    random.shuffle(shuffled_labels)
    idl_count = 0
    for label in shuffled_labels:
        if not object_repository.has_label(label):
            continue
        idls = object_repository.get_all_from_labeled_objects(label).copy()
        idl_count += len(idls)
        random.shuffle(idls)
        for idl in idls:
            # Verify that this object has not already been given a final
            # position by another mechanism or keyword location.
            if idl.instance['debug'].get(DEBUG_FINAL_POSITION_KEY):
                # Exempt place or pickup events on objects already positioned
                # by keyword location.
                if (
                    idl.instance['debug']['positionedBy'] != 'mechanism' and
                    place_or_pickup
                ):
                    return idl
                # Try using another object.
                logger.trace(
                    f'Ignoring existing object {idl.instance["id"]} for use '
                    f'with a new device because it was already given a final '
                    f'position by {idl.instance["debug"]["positionedBy"]}.'
                )
                continue
            return idl
    # If a required object does not exist or was already used by another
    # mechanism, do not generate a new one; just raise an exception.
    # For the TARGET_LABEL, an existing object is always required.
    is_target = (labels == TARGET_LABEL or labels == [TARGET_LABEL])
    if existing_object_required or is_target:
        error_message = (
            f'all {idl_count} matching object(s) are already being used with '
            f'other mechanisms or positioned by keyword locations'
        ) if idl_count else 'no matching object(s) were previously generated'
        raise ILEDelayException(
            f'Failed to find an available object with "{labels=}" for a new '
            f'dropper/placer/thrower because {error_message}.'
        )
    return None


def _get_projectile_idl(
    template: Union[StructuralDropperConfig, StructuralThrowerConfig],
    scene: Scene,
    bounds_list: List[ObjectBounds],
    shapes_to_scales: Dict[str, VectorFloatConfig]
) -> Tuple[InstanceDefinitionLocationTuple, bool]:
    labels = template.projectile_labels if template else None
    if labels:
        idl = _get_existing_held_object_idl(labels)
        if idl:
            logger.trace(
                f'Using existing object for projectile: {idl.instance["id"]}'
            )
            return idl, True

    use_random = template is None or (template.projectile_shape is None and
                                      template.projectile_scale is None and
                                      template.projectile_material is None and
                                      template.projectile_dimensions is None)
    if use_random:
        proj = InteractableObjectConfig(labels=labels)
    else:
        # Choose a shape now, so we can set the default scale accordingly.
        shape, material = choose_shape_material(
            template.projectile_shape,
            template.projectile_material
        )
        # Use shapes_to_scales to choose a default scale for the shape.
        # If the shape isn't in shapes_to_scales, then something is wrong,
        # so throw an error.
        scale = template.projectile_scale or shapes_to_scales[shape]
        dimensions = template.projectile_dimensions or None
        proj = InteractableObjectConfig(
            dimensions=dimensions,
            labels=(
                [label for label in labels if label != TARGET_LABEL]
                if isinstance(labels, list) else labels
            ),
            material=(material.material if material else None),
            shape=shape,
            scale=scale
        )
    srv = InteractableObjectCreationService()
    proj_reconciled = srv.reconcile(scene, proj)
    srv.create_feature_from_specific_values(
        scene, proj_reconciled, proj)
    idl = srv.idl
    idl.instance['debug']['positionedBy'] = 'mechanism'
    idl.instance['debug'][DEBUG_FINAL_POSITION_KEY] = True
    logger.trace(f'Creating new object for projectile: {idl.instance["id"]}')
    return idl, False


def is_wall_too_close(new_wall: SceneObject) -> bool:
    """Return if the given wall object is too close to any existing parallel
    walls in the object repository."""
    new_wall_rotation = round(new_wall['shows'][0]['rotation']['y'], 2)
    # Only run this check if the wall is perfectly horizontal or vertical.
    # TODO Should we check all existing walls that are parallel to this wall,
    #      regardless of starting rotation? We'd need to update the math.
    if new_wall_rotation % 90 != 0:
        return False
    new_wall_is_horizontal = (new_wall_rotation % 180 == 0)
    new_wall_position = new_wall['shows'][0]['position']
    new_wall_scale = new_wall['shows'][0]['scale']
    new_wall_thickness_halved = (new_wall_scale['z'] / 2.0)
    new_wall_width_halved = (new_wall_scale['x'] / 2.0)
    object_repository = ObjectRepository.get_instance()
    walls = object_repository.get_all_from_labeled_objects('walls') or []
    for old_wall in walls:
        old_wall_show = old_wall.instance['shows'][0]
        old_wall_position = old_wall_show['position']
        old_wall_rotation = round(old_wall_show['rotation']['y'], 2)
        # Only check this wall if it's perfectly horizontal or vertical.
        if old_wall_rotation % 90 != 0:
            continue
        old_wall_is_horizontal = (old_wall_rotation % 180 == 0)
        if old_wall_is_horizontal == new_wall_is_horizontal:
            major_axis = 'z' if old_wall_is_horizontal else 'x'
            minor_axis = 'x' if old_wall_is_horizontal else 'z'
            old_wall_scale = old_wall_show['scale']
            old_wall_thickness_halved = (old_wall_scale['z'] / 2.0)
            old_wall_width_halved = (old_wall_scale['x'] / 2.0)
            distance_adjacent = (abs(
                new_wall_position[minor_axis] - old_wall_position[minor_axis]
            ) - old_wall_width_halved - new_wall_width_halved)
            if distance_adjacent > 0:
                continue
            distance_across = (abs(
                new_wall_position[major_axis] - old_wall_position[major_axis]
            ) - old_wall_thickness_halved - new_wall_thickness_halved)
            if distance_across < geometry.PERFORMER_WIDTH:
                return True
    return False


for feature_type, creation_service in [
    (FeatureTypes.DOORS, StructuralDoorsCreationService),
    (FeatureTypes.DROPPERS, StructuralDropperCreationService),
    (FeatureTypes.FLOOR_MATERIALS, StructuralFloorMaterialsCreationService),
    (FeatureTypes.HOLES, StructuralHolesCreationService),
    (FeatureTypes.L_OCCLUDERS, StructuralLOccluderCreationService),
    (FeatureTypes.LAVA, StructuralLavaCreationService),
    (FeatureTypes.PARTITION_FLOOR, StructuralPartitionFloorCreationService),
    (FeatureTypes.MOVING_OCCLUDERS, StructuralMovingOccluderCreationService),
    (FeatureTypes.OCCLUDING_WALLS, StructuralOccludingWallsCreationService),
    (FeatureTypes.PLACERS, StructuralPlacersCreationService),
    (FeatureTypes.PLATFORMS, StructuralPlatformCreationService),
    (FeatureTypes.RAMPS, StructuralRampCreationService),
    (FeatureTypes.THROWERS, StructuralThrowerCreationService),
    (FeatureTypes.WALLS, StructuralWallCreationService),
    (FeatureTypes.TUBE_OCCLUDERS, StructuralTubeOccluderCreationService),
    (FeatureTypes.TURNTABLES, StructuralTurntableCreationService),
    (FeatureTypes.NOTCHED_OCCLUDERS, StructuralNotchedOccluderCreationService)
]:
    FeatureCreationService.register_creation_service(
        feature_type, creation_service)
