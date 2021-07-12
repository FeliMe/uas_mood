from abc import abstractclassmethod
import argparse
from glob import glob
from multiprocessing import Pool
import os
import random

import matplotlib.pyplot as plt
import numpy as np
import torch
from torch.utils.data import Dataset

from uas_mood.utils.artificial_anomalies import sample_complete_mask, patch_exchange
from uas_mood.utils.data_utils import load_segmentation, process_scan


DATAROOT = os.environ.get("DATAROOT")
assert DATAROOT is not None
MOODROOT = os.path.join(DATAROOT, "MOOD")


def plot(images):
    if not isinstance(images, list):
        images = [images]
    n = len(images)
    fig = plt.figure(figsize=(4 * n, 4))
    plt.axis("off")
    for i, im in enumerate(images):
        fig.add_subplot(1, n, i + 1)
        plt.imshow(im, cmap="gray", vmin=0., vmax=1.)
    plt.show()


class PreloadDataset(Dataset):
    def __init__(self):
        super().__init__()

    @staticmethod
    @abstractclassmethod
    def load_batch():
        pass

    def load_to_ram(self, paths, img_size, slices_lower_upper):
        # Set number of cpus used
        num_cpus = os.cpu_count() - 4

        # Split list into batches
        batches = [list(p) for p in np.array_split(
            paths, num_cpus) if len(p) > 0]

        # Start multiprocessing
        with Pool(processes=num_cpus) as pool:
            res = pool.starmap(
                self.load_batch,
                zip(batches, [img_size for _ in batches],
                    [slices_lower_upper for _ in batches])
            )

        return res


class TrainDataset(PreloadDataset):
    def __init__(self, files, img_size, slices_lower_upper):
        super().__init__()
        res = self.load_to_ram(files, img_size, slices_lower_upper)
        samples = [s for r in res for s in r]
        self.samples = [sl for sample in samples for sl in sample]

    def __len__(self):
        return len(self.samples)

    @staticmethod
    def load_batch(files, img_size, slices_lower_upper):
        samples = []
        for f in files:
            # Samples are shape [width, height, slices]
            samples.append(process_scan(f, img_size, equalize_hist=True,
                                        slices_lower_upper=slices_lower_upper))

        return samples

    def __getitem__(self, idx):
        # Select sample
        sample = self.samples[idx]
        # Add fake channels dimension
        sample = sample.unsqueeze(0)
        return sample


class TestDataset(PreloadDataset):
    def __init__(self, files, img_size, slices_lower_upper):
        super().__init__()
        res = self.load_to_ram(files, img_size, slices_lower_upper)
        self.samples = [s for t in res for s in t["samples"]]
        self.segmentations = [s for t in res for s in t["segmentations"]]

    def __len__(self):
        return len(self.samples)

    @staticmethod
    def load_batch(files, img_size, slices_lower_upper):
        samples = []
        segmentations = []
        for f in files:
            # Samples are shape [width, height, slices]
            samples.append((f, process_scan(f, img_size, equalize_hist=True,
                                            slices_lower_upper=slices_lower_upper)))
            # Load segmentation, is in folder test_label/pixel instead of test
            f_seg = f.replace("test", "test_label/pixel")
            segmentations.append(
                (f_seg, load_segmentation(f_seg, img_size,
                                          slices_lower_upper=slices_lower_upper)))

        return {
            "samples": samples,
            "segmentations": segmentations
        }

    def __getitem__(self, idx):
        return self.samples[idx], self.segmentations[idx]


class PatchSwapDataset(PreloadDataset):
    def __init__(self, files, img_size, slices_lower_upper, data):
        super().__init__()
        assert data in ["brain", "abdom"]
        res = self.load_to_ram(files, img_size, slices_lower_upper)
        samples = [s for r in res for s in r]
        self.n_slices = samples[0].shape[0]
        self.n_scans = len(samples)
        self.samples = [sl for sample in samples for sl in sample]
        self.data = data

    def __len__(self):
        return len(self.samples)

    @staticmethod
    def load_batch(files, img_size, slices_lower_upper):
        samples = []
        for f in files:
            # Samples are shape [width, height, slices]
            samples.append(process_scan(f, img_size, equalize_hist=True,
                                        slices_lower_upper=slices_lower_upper))
            if np.any(np.isnan(samples[-1])):
                print(f)

        return samples

    def create_anomaly(self, img1, img2):
        """Create a sample where one patch is switched from another sample
        with a random interpolation factor

        Args:
            img1 (torch.Tensor): shape [w, h]
            img2 (torch.Tensor): shape [w, h]
        """
        mask = sample_complete_mask(
            n_patches=1, blur_prob=0., img=img1, size_range=[0.1, 0.4],
            data=self.data, patch_type="polygon", poly_type="cubic",
            n_vertices=10
        )
        patchex, label = patch_exchange(img1, img2, mask)

        # Convert to tensor
        patchex = torch.from_numpy(patchex)
        label = torch.from_numpy(label)

        return patchex, label

    def __getitem__(self, idx):
        # Select sample
        sample = self.samples[idx].copy()

        # Randomly select another sample at the same slice
        i_slice = idx % self.n_slices
        other_scan = random.randint(0, self.n_scans - 1)
        other_idx = other_scan * self.n_slices + i_slice
        other_sample = self.samples[other_idx]

        # Create foreign patch interpolation
        sample, patch = self.create_anomaly(sample, other_sample)

        # Add fake channels dimension
        sample = sample.unsqueeze(0)
        patch = patch.unsqueeze(0)

        return sample, patch


def get_train_files(root: str, body_region: str):
    """Return all training files

    Args:
        root (str): Smth like $DATAROOT/MOOD/
        body_region (str): One of "brain" or "abdom"
    """
    assert body_region in ["brain", "abdom"]
    return glob(f"{os.path.join(root, body_region, 'train')}/?????.nii.gz")


def get_test_files(root: str, body_region: str):
    """Return all validation or test files

    Args:
        root (str): Smth like $DATAROOT/MOOD/brain/
        body_region (str): One of "brain" or "abdom"
        mode (str): One of "val" or "test"
    """
    assert body_region in ["brain", "abdom"]

    all_files = glob(
        f"{os.path.join(root, body_region, 'test')}/?????_*.nii.gz")
    files = []
    for f in all_files:
        if not f.endswith("_segmentation.nii.gz"):
            files.append(f)

    return files


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    data = "brain"
    # data = "abdom"
    slices_lower_upper = [23, 200] if data == "brain" else [100, 500]
    img_size = 256 if data == "brain" else 512
    train_files = get_train_files(MOODROOT, data)
    test_files = get_test_files(MOODROOT, data)
    print(f"# train_files: {len(train_files)}")
    print(f"# test_files: {len(test_files)}")

    # ----- TrainDataset -----
    # ds = TrainDataset(train_files[:10], 128, slices_lower_upper=[127, 131])
    # x = next(iter(ds))
    # print(x.shape)

    # ----- TestDataset -----
    # ds = TestDataset(test_files[:10], 128, slices_lower_upper=[127, 131])
    # x, y = next(iter(ds))
    # print(x[1].shape, y[1].shape)

    # ----- PatchSwapDataset -----
    ds = PatchSwapDataset(
        train_files[:30], img_size, slices_lower_upper=slices_lower_upper, data=data)
    for x, y in ds:
        print(x.shape)
        print(y.shape, y.min(), y.max())
        print(x.dtype, y.dtype)
        fig = plt.figure(figsize=(8, 4))
        plt.axis('off')
        fig.add_subplot(1, 2, 1)
        plt.imshow(x[0], cmap="gray", vmin=0., vmax=1.)
        fig.add_subplot(1, 2, 2)
        plt.imshow(y[0], cmap="gray", vmin=0., vmax=1.)
        plt.show()
