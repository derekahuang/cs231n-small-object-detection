# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
"""
This file contains specific functions for computing losses on the RPN
file
"""

import torch
from torch.nn import functional as F

from .utils import concat_box_prediction_layers

from ..balanced_positive_negative_sampler import BalancedPositiveNegativeSampler
from ..utils import cat

from maskrcnn_benchmark.layers import smooth_l1_loss
from maskrcnn_benchmark.layers import (
    BinarySigmoidFocalLoss,
    BinarySigmoidReducedFocalLoss,
    BinarySigmoidAreaReducedFocalLoss,
    BinaryAreaLoss,
)
from maskrcnn_benchmark.modeling.matcher import Matcher
from maskrcnn_benchmark.structures.boxlist_ops import boxlist_iou
from maskrcnn_benchmark.structures.boxlist_ops import cat_boxlist


class RPNLossComputation(object):
    """
    This class computes the RPN loss.
    """

    def __init__(self, proposal_matcher, fg_bg_sampler, box_coder,
                 generate_labels_func, objectness_loss):
        """
        Arguments:
            proposal_matcher (Matcher)
            fg_bg_sampler (BalancedPositiveNegativeSampler)
            box_coder (BoxCoder)
            objectness_loss (dict with fn: function, avg: boolean)
        """
        # self.target_preparator = target_preparator
        self.proposal_matcher = proposal_matcher
        self.fg_bg_sampler = fg_bg_sampler
        self.box_coder = box_coder
        self.copied_fields = []
        self.generate_labels_func = generate_labels_func
        self.discard_cases = ['not_visibility', 'between_thresholds']
        self.objectness_loss = objectness_loss

    def match_targets_to_anchors(self, anchor, target, copied_fields=[]):
        match_quality_matrix = boxlist_iou(target, anchor)
        matched_idxs = self.proposal_matcher(match_quality_matrix)
        # RPN doesn't need any fields from target
        # for creating the labels, so clear them all
        target = target.copy_with_fields(copied_fields)
        # get the targets corresponding GT for each anchor
        # NB: need to clamp the indices because we can have a single
        # GT in the image, and matched_idxs can be -2, which goes
        # out of bounds
        matched_targets = target[matched_idxs.clamp(min=0)]
        matched_targets.add_field("matched_idxs", matched_idxs)
        return matched_targets

    def prepare_targets(self, anchors, targets):
        labels = []
        regression_targets = []
        areas = []
        for anchors_per_image, targets_per_image in zip(anchors, targets):
            matched_targets = self.match_targets_to_anchors(
                anchors_per_image, targets_per_image, self.copied_fields
            )

            matched_idxs = matched_targets.get_field("matched_idxs")
            labels_per_image = self.generate_labels_func(matched_targets)
            labels_per_image = labels_per_image.to(dtype=torch.float32)

            # Background (negative examples)
            bg_indices = matched_idxs == Matcher.BELOW_LOW_THRESHOLD
            labels_per_image[bg_indices] = 0

            # discard anchors that go out of the boundaries of the image
            if "not_visibility" in self.discard_cases:
                labels_per_image[~anchors_per_image.get_field("visibility")] = -1

            # discard indices that are between thresholds
            if "between_thresholds" in self.discard_cases:
                inds_to_discard = matched_idxs == Matcher.BETWEEN_THRESHOLDS
                labels_per_image[inds_to_discard] = -1

            # compute regression targets
            regression_targets_per_image = self.box_coder.encode(
                matched_targets.bbox, anchors_per_image.bbox
            )

            areas_per_image = matched_targets.area()

            labels.append(labels_per_image)
            regression_targets.append(regression_targets_per_image)
            areas.append(areas_per_image)

        return labels, regression_targets, areas


    def __call__(self, anchors, objectness, box_regression, targets):
        """
        Arguments:
            anchors (list[BoxList])
            objectness (list[Tensor])
            box_regression (list[Tensor])
            targets (list[BoxList])

        Returns:
            objectness_loss (Tensor)
            box_loss (Tensor
        """
        anchors = [cat_boxlist(anchors_per_image) for anchors_per_image in anchors]
        labels, regression_targets, areas = self.prepare_targets(anchors, targets)
        sampled_pos_inds, sampled_neg_inds = self.fg_bg_sampler(labels)
        sampled_pos_inds = torch.nonzero(torch.cat(sampled_pos_inds, dim=0)).squeeze(1)
        sampled_neg_inds = torch.nonzero(torch.cat(sampled_neg_inds, dim=0)).squeeze(1)

        sampled_inds = torch.cat([sampled_pos_inds, sampled_neg_inds], dim=0)

        objectness, box_regression = \
                concat_box_prediction_layers(objectness, box_regression)

        objectness = objectness.squeeze()

        labels = torch.cat(labels, dim=0)
        regression_targets = torch.cat(regression_targets, dim=0)
        areas = torch.cat(areas, dim=0)

        box_loss = smooth_l1_loss(
            box_regression[sampled_pos_inds],
            regression_targets[sampled_pos_inds],
            beta=1.0 / 9,
            size_average=False,
        ) / (sampled_inds.numel())

        # objectness_loss = objectness_loss_func(
        #     objectness[sampled_inds], labels[sampled_inds]
        # ) / (sampled_inds.numel())

        objectness_loss = self.objectness_loss['fn'](
            objectness[sampled_inds], labels[sampled_inds], areas=areas[sampled_inds]
        )
        if self.objectness_loss['avg']:
            objectness_loss /= sampled_inds.numel()

        return objectness_loss, box_loss

# This function should be overwritten in RetinaNet
def generate_rpn_labels(matched_targets):
    matched_idxs = matched_targets.get_field("matched_idxs")
    labels_per_image = matched_idxs >= 0
    return labels_per_image


def make_rpn_loss_evaluator(cfg, box_coder):
    matcher = Matcher(
        cfg.MODEL.RPN.FG_IOU_THRESHOLD,
        cfg.MODEL.RPN.BG_IOU_THRESHOLD,
        allow_low_quality_matches=True,
    )

    fg_bg_sampler = BalancedPositiveNegativeSampler(
        cfg.MODEL.RPN.BATCH_SIZE_PER_IMAGE, cfg.MODEL.RPN.POSITIVE_FRACTION
    )

    obj_loss_fn_type = cfg.MODEL.RPN.OBJECTNESS_LOSS_FN
    obj_loss = {}
    if obj_loss_fn_type == "BCE":
        obj_loss['fn'] = F.binary_cross_entropy_with_logits
        obj_loss['avg'] = False
    elif obj_loss_fn_type == "Focal":
        obj_loss['fn'] = BinarySigmoidFocalLoss(
            cfg.MODEL.RPN.FOCAL_LOSS_GAMMA,
            cfg.MODEL.RPN.FOCAL_LOSS_ALPHA,
        )
        obj_loss['avg'] = True
    elif obj_loss_fn_type == "ReducedFocal":
        obj_loss['fn'] = BinarySigmoidReducedFocalLoss(
            cfg.MODEL.RPN.FOCAL_LOSS_GAMMA,
            cfg.MODEL.RPN.FOCAL_LOSS_ALPHA,
            cfg.MODEL.RPN.REDUCED_FOCAL_LOSS_CUTOFF,
        )
        obj_loss['avg'] = True
    elif obj_loss_fn_type == "AreaFocal":
        obj_loss['fn'] = BinarySigmoidAreaReducedFocalLoss(
            cfg.MODEL.RPN.FOCAL_LOSS_GAMMA,
            cfg.MODEL.RPN.FOCAL_LOSS_ALPHA,
            cfg.MODEL.RPN.AREA_LOSS_BETA,
            cfg.MODEL.RPN.REDUCED_FOCAL_LOSS_CUTOFF,
            cfg.MODEL.RPN.AREA_LOSS_THRESHOLD,
        )
        obj_loss['avg'] = True
    elif obj_loss_fn_type == "Area":
        obj_loss['fn'] = BinaryAreaLoss(
            cfg.MODEL.RPN.AREA_LOSS_BETA,
            cfg.MODEL.RPN.AREA_LOSS_THRESHOLD,
        )
        obj_loss['avg'] = True
    else:
        raise ValueError("invalid objectness_loss_type: {}".format(obj_loss_fn_type))

    loss_evaluator = RPNLossComputation(
        matcher,
        fg_bg_sampler,
        box_coder,
        generate_rpn_labels,
        obj_loss,
    )
    return loss_evaluator
