import torch
from torch import nn

def sigmoid_area_reduced_focal_loss(logits, targets, gamma, alpha, cutoff):
    num_classes = logits.shape[1]
    gamma = gamma[0]
    alpha = alpha[0]
    cutoff = cutoff[0]
    dtype = targets.dtype
    device = targets.device
    class_range = torch.arange(1, num_classes+1, dtype=dtype, device=device).unsqueeze(0)

    t = targets.unsqueeze(1)
    p = torch.sigmoid(logits)
    term1coef = (p < cutoff).float()*1 \
            + (p >= cutoff).float()*((1.-p)/cutoff)**gamma
    term2coef = (p > 1.-cutoff).float()*1 \
            + (p <= 1.-cutoff).float()*(p/cutoff)**gamma
    term1 = term1coef * torch.log(p)
    term2 = term2coef * torch.log(1 - p)
    return -(t == class_range).float() * term1 * alpha - ((t != class_range) * (t >= 0)).float() * term2 * (1 - alpha)


class SigmoidAreaReducedFocalLoss(nn.Module):
    def __init__(self, gamma, alpha, cutoff):
        super(SigmoidFocalLoss, self).__init__()
        self.gamma = gamma
        self.alpha = alpha
        self.cutoff = cutoff

    def forward(self, logits, targets, areas, **kwargs):
        loss = sigmoid_reduced_focal_loss(logits, targets, areas, self.gamma, self.alpha, self.cutoff)
        return loss.sum()

    def __repr__(self):
        tmpstr = self.__class__.__name__ + "("
        tmpstr += "gamma=" + str(self.gamma)
        tmpstr += ", alpha=" + str(self.alpha)
        tmpstr += ", cutoff=" + str(self.cutoff)
        tmpstr += ")"
        return tmpstr
